import type { PiGatewayClaimResponse } from "./protocol.js";
import { WorkerPool } from "./worker-pool.js";

export interface GatewayControlPlane {
  claim(payload: { capacity: number }): Promise<PiGatewayClaimResponse | undefined>;
  heartbeat(runId: string, attemptId: string, leaseToken: string): Promise<unknown>;
  terminal(
    runId: string,
    attemptId: string,
    outcome: "completed" | "completed_with_warnings" | "failed" | "cancelled",
    leaseToken: string,
    payload?: Record<string, unknown>,
  ): Promise<unknown>;
}

export interface GatewayWorkerHandle {
  abort?: () => void | Promise<void>;
  done?: Promise<void>;
}

export interface PiGatewayOptions {
  controlPlane: GatewayControlPlane;
  capacity: number;
  worker: (claim: PiGatewayClaimResponse) => Promise<void | GatewayWorkerHandle>;
  onError?: (error: unknown) => void;
  heartbeatIntervalMs?: number;
  shutdownTimeoutMs?: number;
}

/** Local fake-friendly Gateway loop; Task 6 adds crash/recovery classification. */
export class PiGateway {
  private readonly controlPlane: GatewayControlPlane;
  private readonly capacity: number;
  private readonly worker: PiGatewayOptions["worker"];
  private readonly onError: (error: unknown) => void;
  private readonly pool: WorkerPool;
  private readonly heartbeatTimers = new Set<ReturnType<typeof setInterval>>();
  private readonly heartbeatIntervalMs: number;
  private readonly shutdownTimeoutMs: number;
  private stopped = false;

  constructor(options: PiGatewayOptions) {
    this.controlPlane = options.controlPlane;
    this.capacity = options.capacity;
    this.worker = options.worker;
    this.onError = options.onError ?? (() => undefined);
    this.heartbeatIntervalMs = Math.max(1, options.heartbeatIntervalMs ?? 20_000);
    this.shutdownTimeoutMs = Math.max(1, options.shutdownTimeoutMs ?? 10_000);
    this.pool = new WorkerPool({ capacity: options.capacity, onWorkerError: this.onError });
  }

  get activeCount(): number {
    return this.pool.activeCount;
  }

  async tick(): Promise<boolean> {
    if (this.stopped || this.activeCount >= this.capacity) return false;
    const claim = await this.controlPlane.claim({ capacity: this.capacity });
    if (!claim) return false;
    const promise = this.pool.submit(async () => {
      let handle: GatewayWorkerHandle | undefined;
      let unregisterAbort: (() => void) | undefined;
      let heartbeatTimer: ReturnType<typeof setInterval> | undefined;
      let rejectHeartbeat!: (error: unknown) => void;
      const heartbeatFailure = new Promise<never>((_resolve, reject) => {
        rejectHeartbeat = reject;
      });
      // The worker may finish initialization without exposing a `done`
      // promise.  In that path the heartbeat failure is handled by the
      // outcome state below rather than by Promise.race; attach a sink now so
      // an early lease loss/cancellation can never become an unhandled
      // rejection.
      void heartbeatFailure.catch(() => undefined);
      let heartbeatFailed = false;
      let heartbeatOutcome: "cancelled" | "lost" | undefined;
      let heartbeatError: unknown;
      try {
        const heartbeat = async (): Promise<void> => {
          if (heartbeatFailed) return;
          try {
            const decision = await this.controlPlane.heartbeat(
              claim.run_id,
              claim.attempt_id,
              claim.lease_token,
            );
            if (
              decision && typeof decision === "object" &&
              "cancel_requested" in decision && decision.cancel_requested === true
            ) {
              heartbeatFailed = true;
              heartbeatOutcome = "cancelled";
              try {
                await handle?.abort?.();
              } finally {
                rejectHeartbeat(new Error("pi_gateway_cancel_requested"));
              }
            }
          } catch (error) {
            heartbeatFailed = true;
            heartbeatOutcome = "lost";
            heartbeatError = error;
            try { await handle?.abort?.(); } finally { rejectHeartbeat(error); }
          }
        };
        // Start renewing before worker/session initialization completes; a
        // slow SDK factory must not let the 60s gateway lease expire.
        heartbeatTimer = setInterval(() => { void heartbeat().catch(() => undefined); }, this.heartbeatIntervalMs);
        heartbeatTimer.unref?.();
        this.heartbeatTimers.add(heartbeatTimer);
        const workerResult = await this.worker(claim);
        handle = typeof workerResult === "object" && workerResult !== null
          ? workerResult
          : undefined;
        if (handle?.abort) unregisterAbort = this.pool.registerAbort(handle.abort);
        if (this.stopped) {
          await handle?.abort?.();
          return;
        }
        if (heartbeatOutcome === "lost") {
          await handle?.abort?.();
          this.onError(heartbeatError ?? new Error("pi_gateway_heartbeat_lost"));
          return;
        }
        if (heartbeatOutcome === "cancelled") {
          await handle?.abort?.();
          await this.controlPlane.terminal(
            claim.run_id,
            claim.attempt_id,
            "cancelled",
            claim.lease_token,
            { code: "cancel_requested" },
          );
          return;
        }
        if (handle?.done) await Promise.race([handle.done, heartbeatFailure]);
        if (this.stopped) return;
        if (heartbeatOutcome === "lost") {
          this.onError(heartbeatError ?? new Error("pi_gateway_heartbeat_lost"));
          return;
        }
        if (heartbeatOutcome === "cancelled") {
          await this.controlPlane.terminal(
            claim.run_id,
            claim.attempt_id,
            "cancelled",
            claim.lease_token,
            { code: "cancel_requested" },
          );
          return;
        }
        await this.controlPlane.terminal(
          claim.run_id,
          claim.attempt_id,
          "completed",
          claim.lease_token,
        );
      } catch (error) {
        if (this.stopped) return;
        if (heartbeatOutcome === "cancelled") {
          try {
            await this.controlPlane.terminal(
              claim.run_id,
              claim.attempt_id,
              "cancelled",
              claim.lease_token,
              { code: "cancel_requested" },
            );
          } catch (terminalError) {
            this.onError(terminalError);
          }
          return;
        }
        if (heartbeatOutcome === "lost") {
          // A lost lease is an infrastructure hand-off.  Leave the Run for
          // backend recovery instead of manufacturing a business failure.
          this.onError(heartbeatError ?? error);
          return;
        }
        this.onError(error);
        try {
          await this.controlPlane.terminal(
            claim.run_id,
            claim.attempt_id,
            "failed",
            claim.lease_token,
            { code: "pi_gateway_worker_failed" },
          );
        } catch (terminalError) {
          this.onError(terminalError);
        }
        throw error;
      } finally {
        if (heartbeatTimer !== undefined) clearInterval(heartbeatTimer);
        if (heartbeatTimer !== undefined) this.heartbeatTimers.delete(heartbeatTimer);
        unregisterAbort?.();
      }
    });
    await promise;
    return true;
  }

  async stop(): Promise<void> {
    if (this.stopped) return;
    this.stopped = true;
    this.pool.setDraining(true);
    await this.pool.abortAll();
    await Promise.race([
      this.pool.waitForIdle(),
      new Promise<void>((resolve) => {
        const timer = setTimeout(resolve, this.shutdownTimeoutMs);
        timer.unref?.();
      }),
    ]);
    for (const timer of this.heartbeatTimers) clearInterval(timer);
    this.heartbeatTimers.clear();
  }
}
