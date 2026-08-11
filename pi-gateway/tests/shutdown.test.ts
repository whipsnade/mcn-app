import { describe, expect, it, vi } from "vitest";

import { PiGateway } from "../src/gateway.js";

describe("PiGateway shutdown", () => {
  it("drains existing workers and does not claim after shutdown begins", async () => {
    let finish!: () => void;
    const done = new Promise<void>((resolve) => { finish = resolve; });
    const abort = vi.fn(() => finish());
    const controlPlane = {
      claim: vi.fn().mockResolvedValue({
        run_id: "run-1",
        attempt_id: "attempt-1",
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
      worker: async () => ({ abort, done }),
    });
    const ticking = gateway.tick();
    await new Promise((resolve) => setTimeout(resolve, 0));
    await gateway.stop();
    await ticking;
    expect(abort).toHaveBeenCalledTimes(1);
    expect(controlPlane.terminal).not.toHaveBeenCalled();
    const calls = controlPlane.claim.mock.calls.length;
    await gateway.tick();
    expect(controlPlane.claim).toHaveBeenCalledTimes(calls);
  });

  it("does not wait forever when a Worker ignores abort", async () => {
    const controlPlane = {
      claim: vi.fn().mockResolvedValue({
        run_id: "run-stuck",
        attempt_id: "attempt-stuck",
        lease_token: "lease-token-with-enough-entropy",
        lease_expires_at: Math.floor(Date.now() / 1000) + 3600,
        runtime_snapshot: {}, transcript: [],
        secret_envelope: { alg: "AES-256-GCM", nonce: "1234567890123456", ciphertext: "1234567890123456" },
        adapter_catalog: [], internal_tools: [],
      }),
      terminal: vi.fn().mockResolvedValue(undefined),
      heartbeat: vi.fn().mockResolvedValue({ cancel_requested: false }),
    };
    const abort = vi.fn();
    const gateway = new PiGateway({
      controlPlane,
      capacity: 1,
      shutdownTimeoutMs: 5,
      worker: async () => ({ abort, done: new Promise<void>(() => undefined) }),
    });
    void gateway.tick();
    await new Promise((resolve) => setTimeout(resolve, 0));
    const started = Date.now();
    await gateway.stop();
    expect(Date.now() - started).toBeLessThan(250);
    expect(abort).toHaveBeenCalledTimes(1);
  });
});
