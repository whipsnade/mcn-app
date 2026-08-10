import type { PiGatewayClaimResponse, PiGatewaySourceEvent } from "./protocol.js";
import { WorkerPool } from "./worker-pool.js";

export type PiGatewayInfrastructureCode =
  | "gateway_lost"
  | "worker_exited"
  | "worker_signaled"
  | "sdk_protocol_error"
  | "control_plane_unreachable"
  | "event_buffer_overflow";

export class PiGatewayInfrastructureError extends Error {
  readonly code: PiGatewayInfrastructureCode;

  constructor(code: PiGatewayInfrastructureCode, cause?: unknown) {
    super(code, { cause });
    this.name = "PiGatewayInfrastructureError";
    this.code = code;
  }
}

const INFRASTRUCTURE_CODES = new Set<PiGatewayInfrastructureCode>([
  "gateway_lost", "worker_exited", "worker_signaled", "sdk_protocol_error",
  "control_plane_unreachable", "event_buffer_overflow",
]);

function errorCode(error: unknown): string | undefined {
  if (!error || typeof error !== "object") return undefined;
  const code = (error as { code?: unknown }).code;
  return typeof code === "string" ? code : undefined;
}

export function asPiGatewayInfrastructureError(error: unknown): PiGatewayInfrastructureError | undefined {
  if (error instanceof PiGatewayInfrastructureError) return error;
  const code = errorCode(error) ?? (error instanceof Error ? error.message : undefined);
  if (code === "pi_gateway_network_error") {
    return new PiGatewayInfrastructureError("control_plane_unreachable", error);
  }
  if (code && INFRASTRUCTURE_CODES.has(code as PiGatewayInfrastructureCode)) {
    return new PiGatewayInfrastructureError(code as PiGatewayInfrastructureCode, error);
  }
  // Worker-entry maps the SDK protocol sentinel to a stable infrastructure code.
  if (code === "sdk_protocol_error") return new PiGatewayInfrastructureError("sdk_protocol_error", error);
  return undefined;
}

/** Bounded in-memory event handoff; overflow is an infrastructure failure. */
export class BoundedGatewayEventBuffer<T = unknown> {
  private readonly events: T[] = [];

  constructor(readonly limit: number) {
    if (!Number.isInteger(limit) || limit < 1 || limit > 100_000) {
      throw new Error("pi_gateway_event_buffer_limit_invalid");
    }
  }

  append(event: T): boolean {
    if (this.events.length >= this.limit) return false;
    this.events.push(event);
    return true;
  }

  get size(): number {
    return this.events.length;
  }

  peek(): T | undefined {
    return this.events[0];
  }

  shift(): T | undefined {
    return this.events.shift();
  }
}

export interface GatewayControlPlane {
  claim(payload: { capacity: number }): Promise<PiGatewayClaimResponse | undefined>;
  heartbeat(runId: string, attemptId: string, leaseToken: string): Promise<unknown>;
  sendEvent?(runId: string, event: PiGatewaySourceEvent, leaseToken: string): Promise<unknown>;
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
  onEvent?: (listener: (event: PiGatewaySourceEvent) => void) => () => void;
}

export interface PiGatewayOptions {
  controlPlane: GatewayControlPlane;
  capacity: number;
  worker: (claim: PiGatewayClaimResponse) => Promise<void | GatewayWorkerHandle>;
  onError?: (error: unknown) => void;
  heartbeatIntervalMs?: number;
  shutdownTimeoutMs?: number;
  maxBufferedEvents?: number;
}

export type PiGatewayDispatch =
  | { outcome: "empty" | "unavailable" }
  | { outcome: "claimed"; completion: Promise<void> };

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
  private readonly maxBufferedEvents: number;
  private stopped = false;

  constructor(options: PiGatewayOptions) {
    this.controlPlane = options.controlPlane;
    this.capacity = options.capacity;
    this.worker = options.worker;
    this.onError = options.onError ?? (() => undefined);
    this.heartbeatIntervalMs = Math.max(1, options.heartbeatIntervalMs ?? 20_000);
    this.shutdownTimeoutMs = Math.max(1, options.shutdownTimeoutMs ?? 10_000);
    this.maxBufferedEvents = options.maxBufferedEvents ?? 256;
    if (!Number.isInteger(this.maxBufferedEvents) || this.maxBufferedEvents < 1 || this.maxBufferedEvents > 100_000) {
      throw new Error("pi_gateway_event_buffer_limit_invalid");
    }
    this.pool = new WorkerPool({ capacity: options.capacity, onWorkerError: this.onError });
  }

  get activeCount(): number {
    return this.pool.activeCount;
  }

  async tick(): Promise<boolean> {
    const dispatch = await this.dispatchNext();
    if (dispatch.outcome !== "claimed") return false;
    await dispatch.completion;
    return true;
  }

  /**
   * Claim at most one Run and dispatch it to the pool without waiting for
   * the worker to finish.  The production run loop uses this to fill shared
   * capacity concurrently; callers must attach a rejection handler to the
   * returned completion promise.
   */
  async dispatchNext(): Promise<PiGatewayDispatch> {
    if (this.stopped || this.activeCount >= this.capacity) return { outcome: "empty" };
    let claim: PiGatewayClaimResponse | undefined;
    try {
      claim = await this.controlPlane.claim({ capacity: this.capacity });
    } catch (error) {
      const infrastructureError = asPiGatewayInfrastructureError(error);
      if (infrastructureError) {
        this.onError(infrastructureError);
        return { outcome: "unavailable" };
      }
      throw error;
    }
    if (!claim) return { outcome: "empty" };
    const promise = this.pool.submit(async () => {
      let handle: GatewayWorkerHandle | undefined;
      let unregisterAbort: (() => void) | undefined;
      let unregisterEvents: (() => void) | undefined;
      let eventBufferError: PiGatewayInfrastructureError | undefined;
      let eventBufferAbort: Promise<void> | undefined;
      const eventBuffer = new BoundedGatewayEventBuffer<PiGatewaySourceEvent>(this.maxBufferedEvents);
      let eventFlushBlocked = false;
      let eventFlushPromise: Promise<void> | undefined;
      const flushEvents = async (): Promise<void> => {
        const sendEvent = this.controlPlane.sendEvent;
        if (!sendEvent || eventFlushBlocked || eventBufferError || eventFlushPromise) return;
        eventFlushPromise = (async () => {
          while (eventBuffer.size > 0 && !eventBufferError) {
            const event = eventBuffer.peek();
            if (!event) return;
            try {
              await sendEvent.call(this.controlPlane, claim.run_id, event, claim.lease_token);
              eventBuffer.shift();
            } catch (error) {
              eventFlushBlocked = true;
              this.onError(asPiGatewayInfrastructureError(error)
                ?? new PiGatewayInfrastructureError("control_plane_unreachable", error));
              return;
            }
          }
        })().finally(() => { eventFlushPromise = undefined; });
        await eventFlushPromise;
      };
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
      // 单次网络抖动/超时不能丢租约：连续失败达到阈值才按 lease 丢失处理。
      // 阈值 × 请求超时必须小于服务端 run lease（默认 60s）。
      let consecutiveHeartbeatFailures = 0;
      const maxConsecutiveHeartbeatFailures = 3;
      try {
        const heartbeat = async (): Promise<void> => {
          if (heartbeatFailed) return;
          try {
            const decision = await this.controlPlane.heartbeat(
              claim.run_id,
              claim.attempt_id,
              claim.lease_token,
            );
            consecutiveHeartbeatFailures = 0;
            if (this.controlPlane.sendEvent && !eventBufferError) {
              eventFlushBlocked = false;
              void flushEvents().catch((error) => this.onError(error));
            }
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
            // 取消语义立即生效；其余错误先计数，连续超阈值才丢租约。
            consecutiveHeartbeatFailures += 1;
            if (consecutiveHeartbeatFailures < maxConsecutiveHeartbeatFailures) {
              this.onError(
                asPiGatewayInfrastructureError(error)
                  ?? new PiGatewayInfrastructureError("control_plane_unreachable", error),
              );
              return;
            }
            heartbeatFailed = true;
            heartbeatOutcome = "lost";
            heartbeatError = asPiGatewayInfrastructureError(error)
              ?? new PiGatewayInfrastructureError("control_plane_unreachable", error);
            try { await handle?.abort?.(); } finally { rejectHeartbeat(heartbeatError); }
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
        if (handle?.onEvent) {
          unregisterEvents = handle.onEvent((event) => {
            if (eventBufferError) return;
            if (eventBuffer.append(event)) {
              void flushEvents().catch((error) => this.onError(error));
              return;
            }
            eventBufferError = new PiGatewayInfrastructureError("event_buffer_overflow");
            eventBufferAbort = Promise.resolve(handle?.abort?.()).catch((error) => {
              this.onError(error);
            });
          });
        }
        if (handle?.abort) unregisterAbort = this.pool.registerAbort(handle.abort);
        if (this.stopped) {
          await handle?.abort?.();
          return;
        }
        if (eventBufferError) {
          await eventBufferAbort;
          this.onError(eventBufferError);
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
        if (this.controlPlane.sendEvent) {
          while (
            eventBuffer.size > 0 &&
            !eventBufferError &&
            !eventFlushBlocked &&
            !heartbeatFailed &&
            !this.stopped
          ) {
            await flushEvents();
            if (
              eventBuffer.size > 0 &&
              !eventBufferError &&
              !eventFlushBlocked &&
              !heartbeatFailed &&
              !this.stopped
            ) {
              await new Promise<void>((resolve) => {
                const timer = setTimeout(resolve, this.heartbeatIntervalMs);
                timer.unref?.();
              });
            }
          }
        }
        if (eventFlushBlocked && eventBuffer.size > 0) {
          // 事件投递在控制面持续失败：中止 worker 并把 Run 留给恢复，
          // 绝不绕过事件顺序伪造 terminal。
          await handle?.abort?.();
          this.onError(new PiGatewayInfrastructureError("control_plane_unreachable"));
          return;
        }
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
        if (eventBufferError) {
          await eventBufferAbort;
          this.onError(eventBufferError);
          return;
        }
        await this.controlPlane.terminal(
          claim.run_id, claim.attempt_id, "completed", claim.lease_token,
        ).catch((error) => {
          const infrastructureError = asPiGatewayInfrastructureError(error);
          if (!infrastructureError) throw error;
          this.onError(infrastructureError);
        });
      } catch (error) {
        if (this.stopped) return;
        if (eventBufferError) {
          await eventBufferAbort;
          this.onError(eventBufferError);
          return;
        }
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
        const infrastructureError = asPiGatewayInfrastructureError(error);
        if (infrastructureError) {
          this.onError(infrastructureError);
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
        unregisterEvents?.();
        unregisterAbort?.();
      }
    });
    return { outcome: "claimed", completion: promise };
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
