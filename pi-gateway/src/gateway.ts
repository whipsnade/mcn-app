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
  heartbeat(
    runId: string,
    attemptId: string,
    leaseToken: string,
    signal?: AbortSignal,
  ): Promise<{ cancel_requested?: boolean; lease_expires_at?: number } | undefined>;
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
  /** 单次控制面请求的墙钟上限；每次 beat 的实际超时还会按租约余额收紧。 */
  controlTimeoutMs?: number;
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
  private readonly heartbeatTimers = new Set<ReturnType<typeof setTimeout>>();
  private readonly heartbeatIntervalMs: number;
  private readonly controlTimeoutMs: number;
  private readonly shutdownTimeoutMs: number;
  private readonly maxBufferedEvents: number;
  private stopped = false;

  constructor(options: PiGatewayOptions) {
    this.controlPlane = options.controlPlane;
    this.capacity = options.capacity;
    this.worker = options.worker;
    this.onError = options.onError ?? (() => undefined);
    this.heartbeatIntervalMs = Math.max(1, options.heartbeatIntervalMs ?? 20_000);
    this.controlTimeoutMs = Math.max(25, options.controlTimeoutMs ?? 15_000);
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
      let heartbeatTimer: ReturnType<typeof setTimeout> | undefined;
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
      let beatsStopped = false;
      let heartbeatOutcome: "cancelled" | "lost" | undefined;
      let heartbeatError: unknown;
      // 租约 fencing：claim 必须携带明确 deadline（协议必填，缺省 fail-closed
      // 不启动 worker）；heartbeat 串行自调度，任何时刻至多一个在飞请求；
      // 单次 beat 超时按租约余额收紧，且「失败重试窗口 + abort grace」严格
      // 小于 lease：maxFailures(2) × lease/4 + lease/4 = 3/4 lease < lease。
      const claimedDeadline = (claim as { lease_expires_at?: unknown }).lease_expires_at;
      if (
        typeof claimedDeadline !== "number" ||
        !Number.isFinite(claimedDeadline) ||
        claimedDeadline <= 0
      ) {
        // 协议违规：不启动 worker，把 Run 留给后端恢复（lease 到期回收）。
        this.onError(new PiGatewayInfrastructureError("control_plane_unreachable"));
        return;
      }
      let leaseDeadlineMs = claimedDeadline * 1000;
      const abortGraceMs = Math.max(
        25,
        Math.min(5_000, Math.floor((leaseDeadlineMs - Date.now()) / 4)),
      );
      const maxConsecutiveHeartbeatFailures = 2;
      let consecutiveHeartbeatFailures = 0;
      let beatInFlight = false;
      const markLost = (cause: unknown): void => {
        if (heartbeatFailed) return;
        heartbeatFailed = true;
        heartbeatOutcome = "lost";
        heartbeatError = asPiGatewayInfrastructureError(cause)
          ?? new PiGatewayInfrastructureError("control_plane_unreachable", cause);
        // abort 等待 Child 真正 close（内部 SIGKILL 升级兜底）后才交还恢复。
        void (async () => {
          try { await handle?.abort?.(); } finally { rejectHeartbeat(heartbeatError); }
        })();
      };
      const scheduleNextBeat = (): void => {
        if (heartbeatFailed || beatsStopped) return;
        const delay = Math.min(
          this.heartbeatIntervalMs,
          Math.max(10, Math.floor((leaseDeadlineMs - Date.now()) / 3)),
        );
        const timer = setTimeout(() => {
          this.heartbeatTimers.delete(timer);
          void beat();
        }, delay);
        timer.unref?.();
        this.heartbeatTimers.add(timer);
        heartbeatTimer = timer;
      };
      const beat = async (): Promise<void> => {
        if (heartbeatFailed || beatInFlight) return;
        beatInFlight = true;
        try {
          const remainingMs = leaseDeadlineMs - Date.now();
          if (remainingMs <= abortGraceMs) {
            // 再发一次也可能在 deadline 之后才被处理：直接放弃租约，留足
            // abort grace，把 Run 交给后端恢复。
            markLost(new Error("pi_gateway_lease_deadline"));
            return;
          }
          const beatTimeoutMs = Math.max(25, Math.min(this.controlTimeoutMs, Math.floor(remainingMs / 4)));
          let beatTimeout: ReturnType<typeof setTimeout> | undefined;
          const beatAbort = new AbortController();
          try {
            const decision = await Promise.race([
              this.controlPlane.heartbeat(
                claim.run_id,
                claim.attempt_id,
                claim.lease_token,
                beatAbort.signal,
              ),
              new Promise<never>((_resolve, reject) => {
                beatTimeout = setTimeout(() => {
                  // 竞速超时必须同时中止底层 fetch，避免服务端在 lost 之后
                  // 仍完成续租。
                  beatAbort.abort();
                  reject(new Error("pi_gateway_heartbeat_timeout"));
                }, beatTimeoutMs);
                beatTimeout.unref?.();
              }),
            ]);
            consecutiveHeartbeatFailures = 0;
            if (decision && typeof decision.lease_expires_at === "number") {
              leaseDeadlineMs = decision.lease_expires_at * 1000;
            }
            if (this.controlPlane.sendEvent && !eventBufferError) {
              eventFlushBlocked = false;
              void flushEvents().catch((error) => this.onError(error));
            }
            if (decision?.cancel_requested === true) {
              heartbeatFailed = true;
              heartbeatOutcome = "cancelled";
              try {
                await handle?.abort?.();
              } finally {
                rejectHeartbeat(new Error("pi_gateway_cancel_requested"));
              }
              return;
            }
          } catch (error) {
            consecutiveHeartbeatFailures += 1;
            this.onError(
              asPiGatewayInfrastructureError(error)
                ?? new PiGatewayInfrastructureError("control_plane_unreachable", error),
            );
            if (
              consecutiveHeartbeatFailures >= maxConsecutiveHeartbeatFailures ||
              Date.now() + abortGraceMs >= leaseDeadlineMs
            ) {
              markLost(error);
              return;
            }
          } finally {
            if (beatTimeout !== undefined) clearTimeout(beatTimeout);
          }
        } finally {
          beatInFlight = false;
          scheduleNextBeat();
        }
      };
      try {
        // Start renewing before worker/session initialization completes; a
        // slow SDK factory must not let the gateway lease expire.
        scheduleNextBeat();
        const workerResult = await this.worker(claim);
        handle = typeof workerResult === "object" && workerResult !== null
          ? workerResult
          : undefined;
        // 任何提前返回路径都不消费 done：先挂 sink，被拒绝的 done 永远不会
        // 成为 unhandledRejection 打挂 Gateway 进程；race 处的 await 语义不变。
        if (handle?.done) void handle.done.catch(() => undefined);
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
          // markLost 可能发生在 worker 初始化完成之前——这里必须兜底 abort
          // 已经 spawn 的 Child，否则孤儿进程会继续经 IPC 桥执行工具调用。
          await handle?.abort?.();
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
        // 先停止再清理：在飞的 beat 结束时会尝试自调度，必须先立停止标志，
        // 否则任务收口后仍会续租，把已结束的 Run 的 lease 永远续下去。
        beatsStopped = true;
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
