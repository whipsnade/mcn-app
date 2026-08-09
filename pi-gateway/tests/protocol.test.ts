import { describe, expect, it } from "vitest";

import { parsePiGatewayClaimResponse, parsePiGatewaySourceEvent } from "../src/protocol.js";

describe("Pi Gateway protocol parser", () => {
  it("rejects identity fields, unknown event types, and overlong deltas", () => {
    const base = { source_event_id: "attempt-1:1", sequence: 1, event_type: "message.delta", payload: { text: "ok" } };
    expect(parsePiGatewaySourceEvent(base)).toEqual(base);
    expect(() => parsePiGatewaySourceEvent({ ...base, payload: { tenant_id: "evil" } })).toThrow("pi_gateway_source_event_invalid");
    expect(() => parsePiGatewaySourceEvent({ ...base, event_type: "arbitrary" })).toThrow("pi_gateway_source_event_invalid");
    expect(() => parsePiGatewaySourceEvent({ ...base, payload: { text: "x".repeat(20_000) } })).toThrow("pi_gateway_source_event_invalid");
    expect(() => parsePiGatewaySourceEvent({ ...base, extra: true })).toThrow("pi_gateway_source_event_invalid");
  });

  it("rejects malformed nested claim response entries", () => {
    const claim = {
      run_id: "run-1",
      attempt_id: "attempt-1",
      lease_token: "lease-token-that-is-long-enough-123",
      runtime_snapshot: {},
      transcript: [{ role: "user", content: "hello" }],
      secret_envelope: { alg: "AES-256-GCM", nonce: "AAAAAAAAAAAAAAAA", ciphertext: "BBBBBBBBBBBBBBBB" },
      adapter_catalog: [],
      internal_tools: [{ name: "load_marketing_skill" }],
    };
    expect(parsePiGatewayClaimResponse(claim).run_id).toBe("run-1");
    expect(() => parsePiGatewayClaimResponse({ ...claim, transcript: [{ role: "user", content: "x", path: "/tmp" }] })).toThrow("pi_gateway_claim_response_invalid");
    expect(() => parsePiGatewayClaimResponse({ ...claim, runtime_snapshot: { token: "secret" } })).toThrow("pi_gateway_claim_response_invalid");
  });
});
