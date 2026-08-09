import { describe, expect, it } from "vitest";

import { ControlPlaneClient } from "../src/control-plane-client.js";


describe("Pi Gateway control-plane client", () => {
  it("signs the exact request and forwards only injected origin", async () => {
    const calls: RequestInit[] = [];
    const client = new ControlPlaneClient({
      origin: "https://control.invalid",
      gatewayId: "gw-1",
      internalSecret: "gateway-secret",
      nonceFactory: () => "nonce-1",
      timestamp: () => 1_700_000_000,
      fetchImpl: async (input, init) => {
        calls.push(init ?? {});
        expect(String(input)).toBe("https://control.invalid/api/v1/internal/pi-gateway/v1/claims");
        return new Response(JSON.stringify({
          run_id: "run-1",
          attempt_id: "attempt-1",
          lease_token: "lease-token-that-is-long-enough-123",
          runtime_snapshot: {},
          transcript: [],
          secret_envelope: { alg: "AES-256-GCM", nonce: "AAAAAAAAAAAAAAAA", ciphertext: "BBBBBBBBBBBBBBBB" },
          adapter_catalog: [],
          internal_tools: [],
        }), { status: 200 });
      },
    });
    const result = await client.claim({ capacity: 1 });
    expect(result?.run_id).toBe("run-1");
    const headers = calls[0].headers as Record<string, string>;
    expect(headers["X-Pi-Gateway-Id"]).toBe("gw-1");
    expect(headers["X-Pi-Nonce"]).toBe("nonce-1");
    expect(headers["Authorization"]).toBeUndefined();
  });

  it("rejects an origin outside the injected HTTPS/loopback boundary", () => {
    expect(() => new ControlPlaneClient({ origin: "https://evil.invalid", gatewayId: "gw", internalSecret: "s" })).not.toThrow();
    expect(() => new ControlPlaneClient({ origin: "http://evil.invalid", gatewayId: "gw", internalSecret: "s" })).toThrow("pi_gateway_origin_invalid");
    expect(() => new ControlPlaneClient({ origin: "https://control.invalid/base", gatewayId: "gw", internalSecret: "s" })).toThrow("pi_gateway_origin_invalid");
    expect(() => new ControlPlaneClient({ origin: "http://127.0.0.1:8080", gatewayId: "gw", internalSecret: "s" })).toThrow("pi_gateway_origin_invalid");
    expect(() => new ControlPlaneClient({ origin: "http://127.0.0.1:8080", environment: "test", gatewayId: "gw", internalSecret: "s" })).not.toThrow();
  });
});
