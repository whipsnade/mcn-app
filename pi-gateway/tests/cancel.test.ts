import { describe, expect, it, vi } from "vitest";

import { PiGateway } from "../src/gateway.js";

describe("Pi Gateway cancellation", () => {
  it("maps an aborted worker to cancelled without a failed terminal", async () => {
    const terminal = vi.fn().mockResolvedValue(undefined);
    const controlPlane = {
      claim: vi.fn().mockResolvedValue({
        run_id: "run-cancel-abort",
        attempt_id: "attempt-cancel-abort",
        lease_token: "lease-token-with-enough-entropy",
        lease_expires_at: Math.floor(Date.now() / 1000) + 3600,
        runtime_snapshot: {}, transcript: [],
        secret_envelope: { alg: "AES-256-GCM", nonce: "1234567890123456", ciphertext: "1234567890123456" },
        adapter_catalog: [], internal_tools: [],
      }),
      heartbeat: vi.fn().mockResolvedValue({ cancel_requested: true }),
      terminal,
    };
    let abortWorker!: () => void;
    const done = new Promise<void>((_resolve, reject) => {
      abortWorker = () => reject(new Error("worker_abort"));
    });
    const gateway = new PiGateway({
      controlPlane,
      capacity: 1,
      heartbeatIntervalMs: 1,
      worker: async () => ({
        abort: vi.fn(() => abortWorker()),
        done,
      }),
    });

    await gateway.tick();

    expect(terminal).toHaveBeenCalledWith(
      "run-cancel-abort",
      "attempt-cancel-abort",
      "cancelled",
      "lease-token-with-enough-entropy",
      { code: "cancel_requested" },
    );
    expect(terminal).not.toHaveBeenCalledWith(
      "run-cancel-abort",
      "attempt-cancel-abort",
      "failed",
      expect.anything(),
      expect.anything(),
    );
  });
});
