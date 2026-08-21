import { describe, expect, it } from "vitest";

import {
  ControlPlaneBusinessError,
  ControlPlaneClient,
  ControlPlaneUnavailableError,
} from "../src/control-plane-client.js";
import { buildProviderFailureMetadata } from "../src/provider-failure.js";


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
          lease_expires_at: 1_700_000_600,
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

  it("classifies transport failure as control_plane_unreachable", async () => {
    const client = new ControlPlaneClient({
      origin: "https://control.invalid",
      gatewayId: "gw-1",
      internalSecret: "gateway-secret",
      fetchImpl: async () => { throw new Error("connection refused"); },
    });

    await expect(client.claim({ capacity: 1 })).rejects.toMatchObject({
      code: "control_plane_unreachable",
      failureClass: "network",
    });
  });

  it("classifies timeout, upstream 5xx and business rejection without exposing bodies", async () => {
    const timeoutClient = new ControlPlaneClient({
      origin: "https://control.invalid",
      gatewayId: "gw-1",
      internalSecret: "gateway-secret",
      timeoutMs: 1,
      fetchImpl: async (_input, init) => new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
      }),
    });
    await expect(timeoutClient.claim({ capacity: 1 })).rejects.toMatchObject({
      code: "control_plane_unreachable",
      failureClass: "timeout",
    });

    const upstreamClient = new ControlPlaneClient({
      origin: "https://control.invalid",
      gatewayId: "gw-1",
      internalSecret: "gateway-secret",
      fetchImpl: async () => new Response("secret response body", { status: 503 }),
    });
    await expect(upstreamClient.claim({ capacity: 1 })).rejects.toMatchObject({
      code: "control_plane_unreachable",
      failureClass: "http_5xx",
      status: 503,
    } satisfies Partial<ControlPlaneUnavailableError>);

    const rejectedClient = new ControlPlaneClient({
      origin: "https://control.invalid",
      gatewayId: "gw-1",
      internalSecret: "gateway-secret",
      fetchImpl: async () => new Response(JSON.stringify({ detail: "pi_gateway_source_sequence_gap" }), { status: 409 }),
    });
    await expect(rejectedClient.claim({ capacity: 1 })).rejects.toBeInstanceOf(ControlPlaneBusinessError);
    await expect(rejectedClient.claim({ capacity: 1 })).rejects.toMatchObject({ status: 409, code: "pi_gateway_source_sequence_gap" });
  });

  it("maps adapter service names back to the backend catalog slug", async () => {
    let requestBody = "";
    const client = new ControlPlaneClient({
      origin: "https://control.invalid",
      gatewayId: "gw-1",
      internalSecret: "gateway-secret",
      fetchImpl: async (_input, init) => {
        requestBody = String(init?.body);
        return new Response(JSON.stringify({ permit_id: "permit-1" }), { status: 200 });
      },
    });
    await client.preflightMcp(
      "run-1",
      { tool: "query_analysis_data", server: "insight-cube", args: {} },
      "lease-token-that-is-long-enough-123",
    );
    expect(JSON.parse(requestBody)).toEqual({
      tool_name: "query_analysis_data",
      server: "insight-cube-mcp",
      args: {},
    });
  });

  it("sends provider failure metadata as a strict top-level terminal field", async () => {
    let requestBody = "";
    const client = new ControlPlaneClient({
      origin: "https://control.invalid",
      gatewayId: "gw-1",
      internalSecret: "gateway-secret",
      fetchImpl: async (_input, init) => {
        requestBody = String(init?.body);
        return new Response("{}", { status: 200 });
      },
    });
    const metadata = buildProviderFailureMetadata(
      { stopReason: "error", errorMessage: "401 Unauthorized" },
      new Date("2026-08-21T08:00:00.000Z"),
    );
    await client.terminal(
      "run-1",
      "attempt-1",
      "failed",
      "lease-token-that-is-long-enough-123",
      { code: "pi_model_provider_error" },
      metadata,
    );
    expect(JSON.parse(requestBody)).toEqual({
      attempt_id: "attempt-1",
      outcome: "failed",
      payload: { code: "pi_model_provider_error" },
      failure_metadata: metadata,
    });
  });

  it("posts bounded source events as one idempotent batch and validates its receipt", async () => {
    let requestPath = "";
    let requestBody = "";
    const client = new ControlPlaneClient({
      origin: "https://control.invalid",
      gatewayId: "gw-1",
      internalSecret: "gateway-secret",
      fetchImpl: async (input, init) => {
        requestPath = new URL(String(input)).pathname;
        requestBody = String(init?.body);
        return new Response(JSON.stringify({
          receipts: [{ source_event_id: "attempt-1:1", sequence: 1, duplicate: false, event_id: "event-1" }],
          last_acked_source_sequence: 1,
        }), { status: 200 });
      },
    });
    const event = { source_event_id: "attempt-1:1", sequence: 1, event_type: "message.start", payload: {} };
    await expect(client.sendEventBatch("run-1", [event], "lease-token-that-is-long-enough-123"))
      .resolves.toMatchObject({ last_acked_source_sequence: 1 });
    expect(requestPath).toBe("/api/v1/internal/pi-gateway/v1/runs/run-1/events/batch");
    expect(JSON.parse(requestBody)).toEqual({ events: [event] });
  });
});
