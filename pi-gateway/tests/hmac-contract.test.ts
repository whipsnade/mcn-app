import { describe, expect, it } from "vitest";

import { buildSignature, ControlPlaneClient } from "../src/control-plane-client.js";
import { CONTROL_PLANE_BASE_PATH } from "../src/protocol.js";

/**
 * Cross-language HMAC contract.  The expected hex digest is generated once by
 * the Python reference implementation (`app.pi_gateway.auth.build_signature`)
 * and pinned here; `backend/tests/pi_gateway/test_auth.py` pins the same
 * fixture from the Python side.  Both sides must sign the full mounted path,
 * never a relative suffix.
 */
const FIXTURE = {
  secret: "cross-language-fixture-secret",
  method: "POST",
  path: "/api/v1/internal/pi-gateway/v1/claims",
  timestamp: 1_700_000_000,
  nonce: "nonce-cross-1",
  body: '{"capacity":2}',
  bodyHash: "3ef882cc0c2405121256f3a69ac414cf77ab59b9bd88252b3de2e28bfc42dfe8",
  signature: "5c0c9eaa797cc4728dfebac69c12de624fbfea50f0f090b99abcd99e020a522c",
} as const;

describe("control-plane HMAC contract", () => {
  it("buildSignature matches the pinned Python reference digest", () => {
    const signature = buildSignature(
      FIXTURE.secret,
      FIXTURE.method,
      FIXTURE.path,
      FIXTURE.timestamp,
      FIXTURE.nonce,
      FIXTURE.body,
    );
    expect(signature).toBe(FIXTURE.signature);
  });

  it("declares the single canonical mounted base path", () => {
    expect(CONTROL_PLANE_BASE_PATH).toBe("/api/v1/internal/pi-gateway/v1");
  });

  it("signs the full mounted path, not a relative suffix", async () => {
    let observed: { url?: string; signature?: string; body?: string } = {};
    const client = new ControlPlaneClient({
      origin: "https://control.invalid",
      gatewayId: "gw-1",
      internalSecret: FIXTURE.secret,
      nonceFactory: () => FIXTURE.nonce,
      timestamp: () => FIXTURE.timestamp,
      fetchImpl: async (input, init) => {
        const headers = (init?.headers ?? {}) as Record<string, string>;
        observed = {
          url: String(input),
          signature: headers["X-Pi-Signature"],
          body: String(init?.body),
        };
        return new Response(null, { status: 204 });
      },
    });

    await client.claim({ capacity: 2 });

    expect(observed.url).toBe("https://control.invalid/api/v1/internal/pi-gateway/v1/claims");
    expect(observed.body).toBe(FIXTURE.body);
    expect(observed.signature).toBe(FIXTURE.signature);
  });

  it("produces a different signature when only the relative suffix is signed", () => {
    const relative = buildSignature(
      FIXTURE.secret,
      FIXTURE.method,
      "/claims",
      FIXTURE.timestamp,
      FIXTURE.nonce,
      FIXTURE.body,
    );
    expect(relative).not.toBe(FIXTURE.signature);
  });
});
