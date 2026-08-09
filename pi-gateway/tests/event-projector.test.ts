import { describe, expect, it } from "vitest";

import { ControlPlaneClient } from "../src/control-plane-client.js";
import { PiSdkUsageProjector, projectPiSdkEvent } from "../src/event-projector.js";

describe("Pi SDK usage projector", () => {
  it("projects provider usage with cache fields and stable source ids", () => {
    const projector = new PiSdkUsageProjector("attempt-1");
    const event = projector.project({
      type: "message_update",
      message: {
        usage: {
          input: 120,
          output: 30,
          cacheRead: 10,
          cacheWrite: 2,
          requestId: "req-1",
        },
      },
    });

    expect(event).toMatchObject({
      source_event_id: "attempt-1:1",
      sequence: 1,
      event_type: "usage",
      payload: {
        input_tokens: 120,
        output_tokens: 30,
        cache_read_tokens: 10,
        cache_write_tokens: 2,
        upstream_request_id: "req-1",
        usage_status: "available",
      },
    });
  });

  it("records unavailable without estimating missing usage and ignores duplicates/unknown events", () => {
    const projector = new PiSdkUsageProjector("attempt-2");
    const first = projector.project({ eventId: "end-1", type: "message_end", message: { role: "assistant" } });
    const duplicate = projector.project({ eventId: "end-1", type: "message_end", message: { role: "assistant" } });
    const unknown = projector.project({ type: "queue_update", steering: [] });

    expect(first).toMatchObject({
      source_event_id: "attempt-2:1",
      payload: { usage_status: "unavailable" },
    });
    expect(first?.payload).not.toHaveProperty("input_tokens");
    expect(duplicate).toBeUndefined();
    expect(unknown).toBeUndefined();
    expect(projector.diagnostics.unknownEvents).toBe(1);
  });

  it("keeps distinct unavailable calls without an SDK identity", () => {
    const projector = new PiSdkUsageProjector("attempt-2b");

    const first = projector.project({ type: "message_end", message: { role: "assistant" } });
    const second = projector.project({ type: "message_end", message: { role: "assistant" } });

    expect(first?.source_event_id).toBe("attempt-2b:1");
    expect(second?.source_event_id).toBe("attempt-2b:2");
    expect(first?.payload).toEqual({ usage_status: "unavailable" });
    expect(second?.payload).toEqual({ usage_status: "unavailable" });
    expect(projector.diagnostics.duplicateUsage).toBe(0);
  });

  it("projects only safe usage fields and suppresses duplicate request ids", () => {
    const projector = new PiSdkUsageProjector("attempt-3");
    const first = projectPiSdkEvent(
      { type: "usage", usage: { input: 1, output: 2, requestId: "req-3", token: "secret" } },
      projector,
    );
    const duplicate = projectPiSdkEvent(
      { type: "usage", usage: { input: 1, output: 2, requestId: "req-3", token: "secret" } },
      projector,
    );

    expect(first?.payload).toEqual({
      input_tokens: 1,
      output_tokens: 2,
      upstream_request_id: "req-3",
      usage_status: "available",
    });
    expect(first?.payload).not.toHaveProperty("token");
    expect(duplicate).toBeUndefined();
  });

  it("sends usage through the authenticated control-plane event path", async () => {
    const calls: RequestInit[] = [];
    const client = new ControlPlaneClient({
      origin: "http://127.0.0.1:8000",
      environment: "test",
      gatewayId: "gateway-1",
      internalSecret: "secret",
      nonceFactory: () => "nonce-usage",
      timestamp: () => 1_700_000_000,
      fetchImpl: async (_url, init) => {
        calls.push(init ?? {});
        return new Response(JSON.stringify({ ok: true }), { status: 200 });
      },
    });
    const projector = new PiSdkUsageProjector("attempt-4");
    const event = projector.project({ type: "usage", usage: { input: 3 } });
    await client.sendUsage("run-1", event!, "lease-token-012345678901234567890123");

    expect(calls).toHaveLength(1);
    expect(JSON.parse(String(calls[0].body))).toMatchObject({ event_type: "usage" });
    expect(calls[0].headers).toMatchObject({ "X-Pi-Run-Lease": "lease-token-012345678901234567890123" });
  });
});
