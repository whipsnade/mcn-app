import { describe, expect, it, vi } from "vitest";

import { PiGateway } from "../src/gateway.js";
import type { PiGatewaySourceEvent } from "../src/protocol.js";

describe("PiGateway", () => {
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
        await new Promise((resolve) => setTimeout(resolve, 5));
        return undefined;
      },
    });

    await gateway.tick();

    expect(onError).toHaveBeenCalled();
    expect(terminal).not.toHaveBeenCalled();
  });

  it("acknowledges a cancellation requested by heartbeat", async () => {
    const terminal = vi.fn().mockResolvedValue(undefined);
    const heartbeat = vi.fn().mockResolvedValue({ cancel_requested: true });
    let finish!: () => void;
    const done = new Promise<void>((resolve) => { finish = resolve; });
    const abort = vi.fn(() => finish());
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
    };
    const gateway = new PiGateway({
      controlPlane, capacity: 1, heartbeatIntervalMs: 1,
      worker: async () => ({ abort, done }),
    });

    await gateway.tick();

    expect(terminal).toHaveBeenCalledWith(
      "run-cancel", "attempt-cancel", "cancelled", "lease-token-with-enough-entropy",
      { code: "cancel_requested" },
    );
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
    };
    const gateway = new PiGateway({
      controlPlane, capacity: 1, heartbeatIntervalMs: 1,
      worker: async () => ({
        done: new Promise<void>((_resolve, reject) =>
          setTimeout(() => reject(new Error("worker_error")), 1)
        ),
      }),
    });

    // 终帧业务错误已成功收口为 failed；pool task 仍会向上抛出该错误供
    // 主循环记录（生产 main 的 tracked completion 捕获）。
    await expect(gateway.tick()).rejects.toThrow("worker_error");

    expect(terminal).toHaveBeenCalledWith(
      "run-biz-fail", "attempt-biz-fail", "failed", "lease-token-with-enough-entropy",
      { code: "pi_gateway_worker_failed" },
    );
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
