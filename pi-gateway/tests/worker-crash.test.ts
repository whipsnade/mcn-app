import { describe, expect, it, vi } from "vitest";

import { PiGateway } from "../src/gateway.js";
import { classifyWorkerError, classifyWorkerExit } from "../src/worker-entry.js";

describe("Pi Gateway worker crash", () => {
  it("classifies child exits with stable infrastructure codes", () => {
    expect(classifyWorkerExit(1, null)).toBe("worker_exited");
    expect(classifyWorkerExit(null, "SIGKILL")).toBe("worker_signaled");
    expect(classifyWorkerError(new Error("sdk_protocol_error"))).toBe("sdk_protocol_error");
    expect(classifyWorkerError(new Error("other"))).toBe("worker_error");
  });

  it("reports a stable worker failure and does not claim success", async () => {
    const terminal = vi.fn().mockResolvedValue(undefined);
    const onError = vi.fn();
    const controlPlane = {
      claim: vi.fn().mockResolvedValue({
        run_id: "run-worker-crash",
        attempt_id: "attempt-worker-crash",
        lease_token: "lease-token-with-enough-entropy",
        lease_expires_at: Math.floor(Date.now() / 1000) + 3600,
        runtime_snapshot: {}, transcript: [],
        secret_envelope: { alg: "AES-256-GCM", nonce: "1234567890123456", ciphertext: "1234567890123456" },
        adapter_catalog: [], internal_tools: [],
      }),
      heartbeat: vi.fn().mockResolvedValue({ cancel_requested: false }),
      terminal,
    };
    const gateway = new PiGateway({
      controlPlane,
      capacity: 1,
      onError,
      worker: async () => { throw new Error("sdk_protocol_error"); },
    });

    await expect(gateway.tick()).resolves.toBe(true);
    expect(onError).toHaveBeenCalledWith(expect.objectContaining({ code: "sdk_protocol_error" }));
    expect(terminal).not.toHaveBeenCalled();
    expect(terminal).not.toHaveBeenCalledWith(
      "run-worker-crash",
      "attempt-worker-crash",
      "completed",
      expect.anything(),
    );
  });
});
