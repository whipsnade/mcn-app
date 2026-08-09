import { describe, expect, it } from "vitest";

import { decryptSecretEnvelope } from "../src/secret-env.js";


describe("secret envelope", () => {
  it("rejects an envelope with a different run/attempt AAD", async () => {
    await expect(decryptSecretEnvelope(
      { alg: "AES-256-GCM", nonce: "AA==", ciphertext: "AA==" },
      "lease",
      { runId: "run-other", attemptId: "attempt", configVersionId: "config", gatewayId: "gw" },
    )).rejects.toThrow("pi_secret_envelope_invalid");
  });
});
