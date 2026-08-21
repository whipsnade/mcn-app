import { describe, expect, it, vi } from "vitest";

import { isPiGatewayTerminalBusinessError, PiGateway } from "../src/gateway.js";
import { PiModelProviderError } from "../src/model-request-budget.js";
import type { PiGatewaySourceEvent } from "../src/protocol.js";

describe("PiGateway", () => {
  it("reproduces event overflow when transient delivery failure waits for heartbeat recovery", async () => {
    const onError = vi.fn();
    const terminal = vi.fn().mockResolvedValue(undefined);
    const heartbeat = vi.fn().mockResolvedValue({ cancel_requested: false });
    const sendEvent = vi.fn()
      .mockRejectedValueOnce(Object.assign(new Error("temporary network"), {
        code: "pi_gateway_network_error",
      }))
      .mockResolvedValue(undefined);
    let emit!: (event: PiGatewaySourceEvent) => void;
    let finish!: () => void;
    const done = new Promise<void>((resolve) => { finish = resolve; });
    const abort = vi.fn(() => finish());
    const controlPlane = {
      claim: vi.fn().mockResolvedValue({
        run_id: "run-event-overflow-repro",
        attempt_id: "attempt-event-overflow-repro",
        lease_token: "lease-token-with-enough-entropy",
        lease_expires_at: Math.floor(Date.now() / 1000) + 3600,
        runtime_snapshot: {}, transcript: [],
        secret_envelope: { alg: "AES-256-GCM", nonce: "1234567890123456", ciphertext: "1234567890123456" },
        adapter_catalog: [], internal_tools: [],
      }),
      terminal,
      heartbeat,
      sendEvent,
    };
    const gateway = new PiGateway({
      controlPlane,
      capacity: 1,
      worker: async () => ({
        abort,
        done,
        onEvent: (listener: (event: PiGatewaySourceEvent) => void) => {
          emit = listener;
          return () => undefined;
        },
      }),
    });

    const tick = gateway.tick();
    while (!emit) await new Promise<void>((resolve) => queueMicrotask(resolve));
    for (let sequence = 1; sequence <= 257; sequence += 1) {
      emit({
        source_event_id: `repro:${sequence}`,
        sequence,
        event_type: "message.start",
        payload: {},
      });
    }

    await expect(tick).resolves.toBe(true);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    expect(heartbeat).not.toHaveBeenCalled();
    expect(sendEvent).toHaveBeenCalledTimes(1);
    expect(abort).toHaveBeenCalledTimes(1);
    expect(terminal).not.toHaveBeenCalled();
  });

  it("passes safe provider failure metadata with the stable business code and no retry", async () => {
    const terminal = vi.fn().mockResolvedValue(undefined);
    const failureMetadata = {
      version: "provider_failure_v1" as const,
      failure_class: "rate_limited" as const,
      http_status: 429,
      error_fingerprint: "a".repeat(64),
    };
    const controlPlane = {
      claim: vi.fn().mockResolvedValue({
        run_id: "run-provider-failure",
        attempt_id: "attempt-provider-failure",
        lease_token: "lease-token-with-enough-entropy",
        lease_expires_at: Math.floor(Date.now() / 1000) + 3600,
        runtime_snapshot: {}, transcript: [],
        secret_envelope: { alg: "AES-256-GCM", nonce: "1234567890123456", ciphertext: "1234567890123456" },
        adapter_catalog: [], internal_tools: [],
      }),
      terminal,
      heartbeat: vi.fn().mockResolvedValue({ cancel_requested: false }),
    };
    const gateway = new PiGateway({
      controlPlane,
      capacity: 1,
      worker: async () => ({
        done: Promise.reject(new PiModelProviderError(failureMetadata)),
      }),
    });

    await expect(gateway.tick()).resolves.toBe(true);
    expect(terminal).toHaveBeenCalledWith(
      "run-provider-failure",
      "attempt-provider-failure",
      "failed",
      "lease-token-with-enough-entropy",
      { code: "pi_model_provider_error" },
      failureMetadata,
    );
    expect(terminal).toHaveBeenCalledTimes(1);
  });

  it("does not report a heartbeat race after terminalization starts", async () => {
    const onError = vi.fn();
    let heartbeatReady!: () => void;
    let rejectHeartbeat!: (error: unknown) => void;
    const heartbeatStarted = new Promise<void>((resolve) => { heartbeatReady = resolve; });
    const heartbeat = vi.fn(() => new Promise<never>((_resolve, reject) => {
      rejectHeartbeat = reject;
      heartbeatReady();
    }));
    const terminal = vi.fn(async () => {
      await heartbeatStarted;
      rejectHeartbeat(Object.assign(new Error("run already terminal"), {
        code: "pi_gateway_run_not_found",
        status: 404,
      }));
    });
    const controlPlane = {
      claim: vi.fn().mockResolvedValue({
        run_id: "run-terminal-heartbeat-race",
        attempt_id: "attempt-terminal-heartbeat-race",
        lease_token: "lease-token-with-enough-entropy",
        lease_expires_at: Math.floor(Date.now() / 1000) + 3600,
        runtime_snapshot: {}, transcript: [],
        secret_envelope: { alg: "AES-256-GCM", nonce: "1234567890123456", ciphertext: "1234567890123456" },
        adapter_catalog: [], internal_tools: [],
      }),
      terminal,
      heartbeat,
    };
    const gateway = new PiGateway({
      controlPlane,
      capacity: 1,
      heartbeatIntervalMs: 1,
      onError,
      worker: async () => {
        await heartbeatStarted;
        return { done: Promise.resolve() };
      },
    });

    await gateway.tick();

    expect(heartbeat).toHaveBeenCalled();
    expect(terminal).toHaveBeenCalledTimes(1);
    expect(onError).not.toHaveBeenCalledWith(
      expect.objectContaining({ code: "control_plane_unreachable" }),
    );
  });

  it("tracks only active isolated worker PIDs", async () => {
    let finish!: () => void;
    const done = new Promise<void>((resolve) => { finish = resolve; });
    const controlPlane = {
      claim: vi.fn().mockResolvedValue({
        run_id: "run-pid",
        attempt_id: "attempt-pid",
        lease_token: "lease-token-with-enough-entropy",
        lease_expires_at: Math.floor(Date.now() / 1000) + 3600,
        runtime_snapshot: {},
        transcript: [],
        secret_envelope: { alg: "AES-256-GCM", nonce: "1234567890123456", ciphertext: "1234567890123456" },
        adapter_catalog: [],
        internal_tools: [],
      }),
      terminal: vi.fn().mockResolvedValue(undefined),
      heartbeat: vi.fn().mockResolvedValue({ cancel_requested: false }),
    };
    const gateway = new PiGateway({
      controlPlane,
      capacity: 1,
      worker: async () => ({ pid: 4242, done }),
    });

    const dispatch = await gateway.dispatchNext();
    expect(dispatch.outcome).toBe("claimed");
    expect(gateway.activeWorkerPids).toEqual([4242]);
    finish();
    await (dispatch as { completion: Promise<void> }).completion;
    expect(gateway.activeWorkerPids).toEqual([]);
  });

  it("claims only up to capacity and sends terminal for completed workers", async () => {
    const terminal = vi.fn().mockResolvedValue(undefined);
    let remaining = 2;
    const controlPlane = {
      claim: vi.fn().mockImplementation(async () => {
        if (remaining === 0) return undefined;
        remaining -= 1;
        return {
          run_id: `run-${remaining}`,
          attempt_id: `attempt-${remaining}`,
          lease_token: "lease-token-with-enough-entropy",
        lease_expires_at: Math.floor(Date.now() / 1000) + 3600,
          runtime_snapshot: {},
          transcript: [],
          secret_envelope: { alg: "AES-256-GCM", nonce: "1234567890123456", ciphertext: "1234567890123456" },
          adapter_catalog: [],
          internal_tools: [],
        };
      }),
      terminal,
      heartbeat: vi.fn().mockResolvedValue({ cancel_requested: false }),
    };
    const gateway = new PiGateway({
      controlPlane,
      capacity: 1,
      worker: async () => undefined,
    });

    await gateway.tick();
    expect(controlPlane.claim).toHaveBeenCalledWith({ capacity: 1 });
    expect(terminal).toHaveBeenCalledTimes(1);
    expect(gateway.activeCount).toBe(0);
  });

  it("renews the lease while a worker is active", async () => {
    const heartbeat = vi.fn().mockResolvedValue({ cancel_requested: false });
    const terminal = vi.fn().mockResolvedValue(undefined);
    const controlPlane = {
      claim: vi.fn().mockResolvedValue({
        run_id: "run-heartbeat",
        attempt_id: "attempt-heartbeat",
        lease_token: "lease-token-with-enough-entropy",
        lease_expires_at: Math.floor(Date.now() / 1000) + 3600,
        runtime_snapshot: {},
        transcript: [],
        secret_envelope: { alg: "AES-256-GCM", nonce: "1234567890123456", ciphertext: "1234567890123456" },
        adapter_catalog: [],
        internal_tools: [],
      }),
      terminal,
      heartbeat,
    };
    const gateway = new PiGateway({
      controlPlane,
      capacity: 1,
      heartbeatIntervalMs: 2,
      worker: async () => ({
        done: new Promise<void>((resolve) => setTimeout(resolve, 12)),
      }),
    });

    await gateway.tick();

    expect(heartbeat).toHaveBeenCalled();
    expect(heartbeat).toHaveBeenCalledWith(
      "run-heartbeat",
      "attempt-heartbeat",
      "lease-token-with-enough-entropy",
      expect.anything(),
    );
    expect(terminal).toHaveBeenCalledWith(
      "run-heartbeat",
      "attempt-heartbeat",
      "completed",
      "lease-token-with-enough-entropy",
    );
  });

  it("leaves the Run for recovery when heartbeat transport is lost", async () => {
    const terminal = vi.fn().mockResolvedValue(undefined);
    const heartbeat = vi.fn().mockRejectedValue(new Error("network down"));
    let finish!: () => void;
    const done = new Promise<void>((resolve) => { finish = resolve; });
    const abort = vi.fn(() => finish());
    const controlPlane = {
      claim: vi.fn().mockResolvedValue({
        run_id: "run-lost",
        attempt_id: "attempt-lost",
        lease_token: "lease-token-with-enough-entropy",
        lease_expires_at: Math.floor(Date.now() / 1000) + 3600,
        runtime_snapshot: {}, transcript: [],
        secret_envelope: { alg: "AES-256-GCM", nonce: "1234567890123456", ciphertext: "1234567890123456" },
        adapter_catalog: [], internal_tools: [],
      }),
      terminal,
      heartbeat,
    };
    const gateway = new PiGateway({
      controlPlane, capacity: 1, heartbeatIntervalMs: 1,
      worker: async () => ({ abort, done }),
    });

    await gateway.tick();

    expect(abort).toHaveBeenCalledTimes(1);
    expect(terminal).not.toHaveBeenCalled();
  });

  it("consumes an early heartbeat failure when initialization has no done handle", async () => {
    const terminal = vi.fn().mockResolvedValue(undefined);
    const onError = vi.fn();
    const controlPlane = {
      claim: vi.fn().mockResolvedValue({
        run_id: "run-init-lost",
        attempt_id: "attempt-init-lost",
        lease_token: "lease-token-with-enough-entropy",
        lease_expires_at: Math.floor(Date.now() / 1000) + 3600,
        runtime_snapshot: {}, transcript: [],
        secret_envelope: { alg: "AES-256-GCM", nonce: "1234567890123456", ciphertext: "1234567890123456" },
        adapter_catalog: [], internal_tools: [],
      }),
      terminal,
      heartbeat: vi.fn().mockRejectedValue(new Error("network down")),
    };

    const gateway = new PiGateway({
      controlPlane,
      capacity: 1,
      heartbeatIntervalMs: 1,
      onError,
      worker: async () => {
        // 两次 heartbeat failure 才会触发 lease hand-off；给 1ms heartbeat
        // 留出稳定的调度窗口，避免 CI 冷启动时 worker 先完成而掩盖租约丢失。
        await new Promise((resolve) => setTimeout(resolve, 50));
        return undefined;
      },
    });

    await gateway.tick();

    expect(onError).toHaveBeenCalled();
    expect(terminal).not.toHaveBeenCalled();
  });

  it("acknowledges a cancellation requested by heartbeat", async () => {
    const terminal = vi.fn().mockResolvedValue(undefined);
    const order: string[] = [];
    const sendEvent = vi.fn(async () => { order.push("event"); });
    const heartbeat = vi.fn().mockResolvedValue({ cancel_requested: true });
    let finish!: () => void;
    const done = new Promise<void>((resolve) => { finish = resolve; });
    let emit!: (event: PiGatewaySourceEvent) => void;
    const abort = vi.fn(() => {
      emit({ source_event_id: "cancel:1", sequence: 1, event_type: "message.completed", payload: { text: "已取消前的答复" } });
      finish();
    });
    const controlPlane = {
      claim: vi.fn().mockResolvedValue({
        run_id: "run-cancel",
        attempt_id: "attempt-cancel",
        lease_token: "lease-token-with-enough-entropy",
        lease_expires_at: Math.floor(Date.now() / 1000) + 3600,
        runtime_snapshot: {}, transcript: [],
        secret_envelope: { alg: "AES-256-GCM", nonce: "1234567890123456", ciphertext: "1234567890123456" },
        adapter_catalog: [], internal_tools: [],
      }),
      terminal,
      heartbeat,
      sendEvent,
    };
    const gateway = new PiGateway({
      controlPlane, capacity: 1, heartbeatIntervalMs: 1,
      worker: async () => ({
        abort,
        done,
        onEvent: (listener: (event: PiGatewaySourceEvent) => void) => {
          emit = listener;
          return () => undefined;
        },
      }),
    });

    await gateway.tick();

    expect(terminal).toHaveBeenCalledWith(
      "run-cancel", "attempt-cancel", "cancelled", "lease-token-with-enough-entropy",
      { code: "cancel_requested" },
    );
    order.push("terminal");
    expect(order).toEqual(["event", "terminal"]);
  });

  it("leaves a Run for recovery when claim transport is unreachable", async () => {
    const onError = vi.fn();
    const terminal = vi.fn();
    const controlPlane = {
      claim: vi.fn().mockRejectedValue(Object.assign(new Error("offline"), {
        code: "control_plane_unreachable",
      })),
      terminal,
      heartbeat: vi.fn(),
    };
    const gateway = new PiGateway({
      controlPlane, capacity: 1, onError, worker: async () => undefined,
    });

    await expect(gateway.tick()).resolves.toBe(false);

    expect(onError).toHaveBeenCalledWith(expect.objectContaining({ code: "control_plane_unreachable" }));
    expect(terminal).not.toHaveBeenCalled();
  });

  it("does not manufacture a failed Run when terminal transport is unreachable", async () => {
    const onError = vi.fn();
    const terminal = vi.fn().mockRejectedValue(Object.assign(new Error("offline"), {
      code: "control_plane_unreachable",
    }));
    const controlPlane = {
      claim: vi.fn().mockResolvedValue({
        run_id: "run-terminal-lost",
        attempt_id: "attempt-terminal-lost",
        lease_token: "lease-token-with-enough-entropy",
        lease_expires_at: Math.floor(Date.now() / 1000) + 3600,
        runtime_snapshot: {}, transcript: [],
        secret_envelope: { alg: "AES-256-GCM", nonce: "1234567890123456", ciphertext: "1234567890123456" },
        adapter_catalog: [], internal_tools: [],
      }),
      terminal,
      heartbeat: vi.fn().mockResolvedValue({ cancel_requested: false }),
    };
    const gateway = new PiGateway({
      controlPlane, capacity: 1, onError, worker: async () => undefined,
    });

    await expect(gateway.tick()).resolves.toBe(true);

    expect(terminal).toHaveBeenCalledTimes(1);
    expect(onError).toHaveBeenCalledWith(expect.objectContaining({ code: "control_plane_unreachable" }));
  });

  it("aborts when the local event buffer reaches its bound", async () => {
    const onError = vi.fn();
    const terminal = vi.fn();
    const controlPlane = {
      claim: vi.fn().mockResolvedValue({
        run_id: "run-event-overflow",
        attempt_id: "attempt-event-overflow",
        lease_token: "lease-token-with-enough-entropy",
        lease_expires_at: Math.floor(Date.now() / 1000) + 3600,
        runtime_snapshot: {}, transcript: [],
        secret_envelope: { alg: "AES-256-GCM", nonce: "1234567890123456", ciphertext: "1234567890123456" },
        adapter_catalog: [], internal_tools: [],
      }),
      terminal,
      heartbeat: vi.fn().mockResolvedValue({ cancel_requested: false }),
    };
    let emit!: (event: PiGatewaySourceEvent) => void;
    let finish!: () => void;
    const done = new Promise<void>((resolve) => { finish = resolve; });
    const abort = vi.fn(() => finish());
    const gateway = new PiGateway({
      controlPlane, capacity: 1, onError, maxBufferedEvents: 1,
      worker: async () => ({
        abort,
        done,
        onEvent: (listener: (event: PiGatewaySourceEvent) => void) => {
          emit = listener;
          return () => undefined;
        },
      }),
    });

    const tick = gateway.tick();
    await new Promise<void>((resolve) => queueMicrotask(() => resolve()));
    emit({ source_event_id: "event:1", sequence: 1, event_type: "message.start", payload: {} });
    emit({ source_event_id: "event:2", sequence: 2, event_type: "message.end", payload: {} });
    await expect(tick).resolves.toBe(true);

    expect(abort).toHaveBeenCalledTimes(1);
    expect(onError).toHaveBeenCalledWith(expect.objectContaining({ code: "event_buffer_overflow" }));
    expect(terminal).not.toHaveBeenCalled();
  });

  it("flushes buffered source events through the control plane", async () => {
    const terminal = vi.fn().mockResolvedValue(undefined);
    const sendEvent = vi.fn().mockResolvedValue(undefined);
    const controlPlane = {
      claim: vi.fn().mockResolvedValue({
        run_id: "run-event-flush",
        attempt_id: "attempt-event-flush",
        lease_token: "lease-token-with-enough-entropy",
        lease_expires_at: Math.floor(Date.now() / 1000) + 3600,
        runtime_snapshot: {}, transcript: [],
        secret_envelope: { alg: "AES-256-GCM", nonce: "1234567890123456", ciphertext: "BBBBBBBBBBBBBBBB" },
        adapter_catalog: [], internal_tools: [],
      }),
      terminal,
      heartbeat: vi.fn().mockResolvedValue({ cancel_requested: false }),
      sendEvent,
    };
    let emit!: (event: {
      source_event_id: string;
      sequence: number;
      event_type: string;
      payload: Record<string, unknown>;
    }) => void;
    const gateway = new PiGateway({
      controlPlane, capacity: 1,
      worker: async () => ({
        onEvent: (listener: typeof emit) => {
          emit = listener;
          return () => undefined;
        },
      }),
    });

    const tick = gateway.tick();
    while (!emit) await new Promise<void>((resolve) => queueMicrotask(resolve));
    emit({
      source_event_id: "event:1",
      sequence: 1,
      event_type: "message.start",
      payload: { text: "hello" },
    });
    await tick;

    expect(sendEvent).toHaveBeenCalledWith(
      "run-event-flush",
      {
        source_event_id: "event:1",
        sequence: 1,
        event_type: "message.start",
        payload: { text: "hello" },
      },
      "lease-token-with-enough-entropy",
    );
  });

  it("posts a failed terminal for child business errors without touching recovery", async () => {
    // 子进程终帧的业务错误（worker_error）不属于基础设施故障：直接 failed
    // 收口，不留给恢复、不消耗唯一的一次基础设施重试。
    const terminal = vi.fn().mockResolvedValue(undefined);
    const order: string[] = [];
    const sendEvent = vi.fn(async () => { order.push("event"); });
    let emit!: (event: PiGatewaySourceEvent) => void;
    const controlPlane = {
      claim: vi.fn().mockResolvedValue({
        run_id: "run-biz-fail",
        attempt_id: "attempt-biz-fail",
        lease_token: "lease-token-with-enough-entropy",
        lease_expires_at: Math.floor(Date.now() / 1000) + 3600,
        runtime_snapshot: {}, transcript: [],
        secret_envelope: { alg: "AES-256-GCM", nonce: "1234567890123456", ciphertext: "1234567890123456" },
        adapter_catalog: [], internal_tools: [],
      }),
      terminal,
      heartbeat: vi.fn().mockResolvedValue({ cancel_requested: false }),
      sendEvent,
    };
    const done = new Promise<void>((_resolve, reject) =>
      setTimeout(() => {
        emit({ source_event_id: "business:1", sequence: 1, event_type: "message.completed", payload: { text: "失败前的说明" } });
        reject(new Error("worker_error"));
      }, 1),
    );
    const gateway = new PiGateway({
      controlPlane, capacity: 1, heartbeatIntervalMs: 1,
      worker: async () => ({
        done,
        onEvent: (listener: (event: PiGatewaySourceEvent) => void) => {
          emit = listener;
          return () => undefined;
        },
      }),
    });

    // 终帧业务错误已成功收口为 failed；pool task 仍会向上抛出该错误供
    // 主循环记录（生产 main 的 tracked completion 捕获）。
    await expect(gateway.tick()).rejects.toThrow("worker_error");

    expect(terminal).toHaveBeenCalledWith(
      "run-biz-fail", "attempt-biz-fail", "failed", "lease-token-with-enough-entropy",
      { code: "pi_gateway_worker_failed" },
    );
    order.push("terminal");
    expect(order).toEqual(["event", "terminal"]);
  });

  it("leaves a signaled worker for recovery without posting a terminal", async () => {
    // 无终帧的信号死亡（worker_signaled）是基础设施故障：不得伪造业务
    // failed，交给后端恢复创建一次新 Attempt。
    const terminal = vi.fn().mockResolvedValue(undefined);
    const onError = vi.fn();
    const controlPlane = {
      claim: vi.fn().mockResolvedValue({
        run_id: "run-signaled",
        attempt_id: "attempt-signaled",
        lease_token: "lease-token-with-enough-entropy",
        lease_expires_at: Math.floor(Date.now() / 1000) + 3600,
        runtime_snapshot: {}, transcript: [],
        secret_envelope: { alg: "AES-256-GCM", nonce: "1234567890123456", ciphertext: "1234567890123456" },
        adapter_catalog: [], internal_tools: [],
      }),
      terminal,
      heartbeat: vi.fn().mockResolvedValue({ cancel_requested: false }),
    };
    const gateway = new PiGateway({
      controlPlane, capacity: 1, heartbeatIntervalMs: 1, onError,
      worker: async () => ({
        done: new Promise<void>((_resolve, reject) =>
          setTimeout(() => reject(new Error("worker_signaled")), 1)
        ),
      }),
    });

    await gateway.tick();

    expect(terminal).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalled();
  });

  it("never leaves a rejected done promise unhandled on early-return paths", async () => {
    // abort 等 close 后，提前返回路径（lease lost 等）不给 done 挂 handler
    // 会让被拒绝的 done 成为 unhandledRejection，生产上会打挂 Gateway 进程。
    const terminal = vi.fn().mockResolvedValue(undefined);
    const onError = vi.fn();
    const heartbeat = vi.fn().mockRejectedValue(
      Object.assign(new Error("offline"), { code: "control_plane_unreachable" }),
    );
    const controlPlane = {
      claim: vi.fn().mockResolvedValue({
        run_id: "run-unhandled",
        attempt_id: "attempt-unhandled",
        lease_token: "lease-token-with-enough-entropy",
        lease_expires_at: Math.floor(Date.now() / 1000) + 3600,
        runtime_snapshot: {}, transcript: [],
        secret_envelope: { alg: "AES-256-GCM", nonce: "1234567890123456", ciphertext: "1234567890123456" },
        adapter_catalog: [], internal_tools: [],
      }),
      terminal,
      heartbeat,
    };
    let unhandled: unknown;
    const onUnhandled = (reason: unknown): void => { unhandled = reason; };
    process.once("unhandledRejection", onUnhandled);
    try {
      const gateway = new PiGateway({
        controlPlane, capacity: 1, heartbeatIntervalMs: 1, onError,
        worker: async () => ({
          // abort 不触碰 done：模拟 close 后拒绝（worker_exited）
          abort: async () => undefined,
          done: new Promise<void>((_resolve, reject) =>
            setTimeout(() => reject(new Error("worker_exited")), 5)
          ),
        }),
      });
      await gateway.tick();
      await new Promise<void>((resolve) => setTimeout(resolve, 30));
      expect(unhandled).toBeUndefined();
    } finally {
      process.removeListener("unhandledRejection", onUnhandled);
    }
  });

  it("serializes heartbeats so a slow beat never overlaps the next one", async () => {
    // 租约 fencing：同一 Run 的 heartbeat 必须串行，慢请求不得与下一次重叠。
    let inFlight = 0;
    let maxInFlight = 0;
    const pending: Array<() => void> = [];
    const heartbeat = vi.fn(() => {
      inFlight += 1;
      maxInFlight = Math.max(maxInFlight, inFlight);
      return new Promise<{ cancel_requested: boolean; lease_expires_at: number }>((resolve) => {
        pending.push(() => {
          inFlight -= 1;
          resolve({
            cancel_requested: false,
            lease_expires_at: Math.floor(Date.now() / 1000) + 3600,
          });
        });
      });
    });
    const terminal = vi.fn().mockResolvedValue(undefined);
    const controlPlane = {
      claim: vi.fn().mockResolvedValue({
        run_id: "run-serial-hb",
        attempt_id: "attempt-serial-hb",
        lease_token: "lease-token-with-enough-entropy",
        lease_expires_at: Math.floor(Date.now() / 1000) + 3600,
        runtime_snapshot: {}, transcript: [],
        secret_envelope: { alg: "AES-256-GCM", nonce: "1234567890123456", ciphertext: "1234567890123456" },
        adapter_catalog: [], internal_tools: [],
      }),
      terminal,
      heartbeat,
    };
    const gateway = new PiGateway({
      controlPlane, capacity: 1, heartbeatIntervalMs: 1,
      worker: async () => ({ done: new Promise<void>((resolve) => setTimeout(resolve, 25)) }),
    });

    const tick = gateway.tick();
    await new Promise<void>((resolve) => setTimeout(resolve, 10));
    for (const release of pending.splice(0)) release();
    await tick;

    expect(maxInFlight).toBe(1);
    expect(heartbeat).toHaveBeenCalled();
  });

  it("fails closed when the claim carries no explicit lease deadline", async () => {
    const onError = vi.fn();
    const worker = vi.fn();
    const controlPlane = {
      claim: vi.fn().mockResolvedValue({
        run_id: "run-no-deadline",
        attempt_id: "attempt-no-deadline",
        lease_token: "lease-token-with-enough-entropy",
        runtime_snapshot: {}, transcript: [],
        secret_envelope: { alg: "AES-256-GCM", nonce: "1234567890123456", ciphertext: "1234567890123456" },
        adapter_catalog: [], internal_tools: [],
      }),
      terminal: vi.fn(),
      heartbeat: vi.fn(),
    };
    const gateway = new PiGateway({ controlPlane, capacity: 1, onError, worker });

    await gateway.tick();

    expect(worker).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalled();
  });

  it("allows a bounded slow heartbeat without aborting before the lease grace", async () => {
    const terminal = vi.fn().mockResolvedValue(undefined);
    const heartbeat = vi.fn(async () => {
      await new Promise<void>((resolve) => setTimeout(resolve, 1_200));
      return {
        cancel_requested: false,
        lease_expires_at: Math.floor(Date.now() / 1000) + 3,
      };
    });
    const controlPlane = {
      claim: vi.fn().mockResolvedValue({
        run_id: "run-slow-heartbeat",
        attempt_id: "attempt-slow-heartbeat",
        lease_token: "lease-token-with-enough-entropy",
        lease_expires_at: Date.now() / 1000 + 3,
        runtime_snapshot: {}, transcript: [],
        secret_envelope: { alg: "AES-256-GCM", nonce: "1234567890123456", ciphertext: "1234567890123456" },
        adapter_catalog: [], internal_tools: [],
      }),
      terminal,
      heartbeat,
    };
    const gateway = new PiGateway({
      controlPlane,
      capacity: 1,
      heartbeatIntervalMs: 5,
      worker: async () => ({
        done: new Promise<void>((resolve) => setTimeout(resolve, 1_400)),
      }),
    });

    await gateway.tick();

    expect(heartbeat).toHaveBeenCalled();
    expect(terminal).toHaveBeenCalledWith(
      "run-slow-heartbeat",
      "attempt-slow-heartbeat",
      "completed",
      "lease-token-with-enough-entropy",
    );
  });

  it("declares the lease lost at the deadline and aborts the worker inside the grace", async () => {
    // heartbeat 永不返回：deadline 到达时（预留 abort grace）必须放弃租约并
    // 中止 worker，把 Run 留给恢复，而不是等超时拖过 lease。
    const heartbeat = vi.fn(() => new Promise<never>(() => undefined));
    const terminal = vi.fn().mockResolvedValue(undefined);
    let finish!: () => void;
    const done = new Promise<void>((resolve) => { finish = resolve; });
    const abort = vi.fn(async () => { finish(); });
    const controlPlane = {
      claim: vi.fn().mockResolvedValue({
        run_id: "run-deadline",
        attempt_id: "attempt-deadline",
        lease_token: "lease-token-with-enough-entropy",
        lease_expires_at: Date.now() / 1000 + 0.3,
        runtime_snapshot: {}, transcript: [],
        secret_envelope: { alg: "AES-256-GCM", nonce: "1234567890123456", ciphertext: "1234567890123456" },
        adapter_catalog: [], internal_tools: [],
      }),
      terminal,
      heartbeat,
    };
    const gateway = new PiGateway({
      controlPlane, capacity: 1, heartbeatIntervalMs: 5,
      worker: async () => ({ abort, done }),
    });

    await gateway.tick();

    expect(abort).toHaveBeenCalledTimes(1);
    expect(terminal).not.toHaveBeenCalled();
  });
});

describe("pi_decision_limit business terminal", () => {
  it("settles failed with code pi_decision_limit (no recovery, no opaque worker_failed)", async () => {
    const terminal = vi.fn().mockResolvedValue(undefined);
    const heartbeat = vi.fn().mockResolvedValue({ lease_expires_at: Math.floor(Date.now() / 1000) + 3600 });
    const onError = vi.fn();
    const controlPlane = {
      claim: vi.fn().mockResolvedValue({
        run_id: "run-budget",
        attempt_id: "attempt-budget",
        lease_token: "lease-token-with-enough-entropy",
        lease_expires_at: Math.floor(Date.now() / 1000) + 3600,
        runtime_snapshot: {}, transcript: [],
        secret_envelope: { alg: "AES-256-GCM", nonce: "1234567890123456", ciphertext: "1234567890123456" },
        adapter_catalog: [], internal_tools: [],
      }),
      terminal,
      heartbeat,
    };
    const gateway = new PiGateway({
      controlPlane, capacity: 1, heartbeatIntervalMs: 1, onError,
      worker: async () => ({
        done: Promise.reject(new Error("pi_decision_limit")),
      }),
    });

    await gateway.tick();

    expect(terminal).toHaveBeenCalledTimes(1);
    expect(terminal).toHaveBeenCalledWith(
      "run-budget",
      "attempt-budget",
      "failed",
      "lease-token-with-enough-entropy",
      { code: "pi_decision_limit" },
    );
    // 业务预算终止不得进入基础设施错误通道（那会把 Run 留给恢复/重放）。
    for (const call of onError.mock.calls) {
      expect(String(call[0])).not.toContain("PiGatewayInfrastructureError");
    }
  });
});

describe("completion and loop-guard business terminal", () => {
  it("converts a server-owned runtime snapshot rejection into stable failed without recovery", async () => {
    const terminal = vi.fn().mockResolvedValue(undefined);
    const heartbeat = vi.fn().mockResolvedValue({
      lease_expires_at: Math.floor(Date.now() / 1000) + 3600,
    });
    const onError = vi.fn();
    const controlPlane = {
      claim: vi.fn().mockResolvedValue({
        run_id: "run-invalid-snapshot",
        attempt_id: "attempt-invalid-snapshot",
        lease_token: "lease-token-with-enough-entropy",
        lease_expires_at: Math.floor(Date.now() / 1000) + 3600,
        runtime_snapshot: {},
        transcript: [],
        secret_envelope: { alg: "AES-256-GCM", nonce: "1234567890123456", ciphertext: "1234567890123456" },
        adapter_catalog: [],
        internal_tools: [],
      }),
      terminal,
      heartbeat,
    };
    const gateway = new PiGateway({
      controlPlane, capacity: 1, heartbeatIntervalMs: 1, onError,
      worker: async () => ({ done: Promise.reject(new Error("pi_gateway_runtime_snapshot_invalid")) }),
    });

    await gateway.tick();

    expect(terminal).toHaveBeenCalledWith(
      "run-invalid-snapshot",
      "attempt-invalid-snapshot",
      "failed",
      "lease-token-with-enough-entropy",
      { code: "pi_gateway_runtime_snapshot_invalid" },
    );
    expect(terminal).not.toHaveBeenCalledWith(
      expect.anything(), expect.anything(), expect.anything(), expect.anything(),
      { code: "pi_gateway_worker_failed" },
    );
    expect(onError).not.toHaveBeenCalled();
  });

  it("converts a completion gate rejection into stable failed without worker_failed", async () => {
    const terminal = vi.fn()
      .mockRejectedValueOnce(Object.assign(new Error("pi_gateway_artifact_invalid"), {
        code: "pi_gateway_artifact_invalid",
        status: 409,
      }))
      .mockResolvedValueOnce(undefined);
    const heartbeat = vi.fn().mockResolvedValue({
      lease_expires_at: Math.floor(Date.now() / 1000) + 3600,
    });
    const onError = vi.fn();
    const controlPlane = {
      claim: vi.fn().mockResolvedValue({
        run_id: "run-artifact-gate",
        attempt_id: "attempt-artifact-gate",
        lease_token: "lease-token-with-enough-entropy",
        lease_expires_at: Math.floor(Date.now() / 1000) + 3600,
        runtime_snapshot: {}, transcript: [],
        secret_envelope: { alg: "AES-256-GCM", nonce: "1234567890123456", ciphertext: "1234567890123456" },
        adapter_catalog: [], internal_tools: [],
      }),
      terminal,
      heartbeat,
    };
    const gateway = new PiGateway({
      controlPlane, capacity: 1, heartbeatIntervalMs: 1, onError,
      worker: async () => ({ done: Promise.resolve() }),
    });

    await gateway.tick();

    expect(terminal).toHaveBeenNthCalledWith(
      1,
      "run-artifact-gate",
      "attempt-artifact-gate",
      "completed",
      "lease-token-with-enough-entropy",
    );
    expect(terminal).toHaveBeenNthCalledWith(
      2,
      "run-artifact-gate",
      "attempt-artifact-gate",
      "failed",
      "lease-token-with-enough-entropy",
      { code: "pi_gateway_artifact_invalid" },
    );
    expect(onError).not.toHaveBeenCalled();
  });

  it("does not classify a legacy loop warning as a terminal business error", () => {
    expect(isPiGatewayTerminalBusinessError(Object.assign(new Error("agent_loop_circuit_open"), {
      code: "agent_loop_circuit_open",
    }))).toBe(false);
  });
});
