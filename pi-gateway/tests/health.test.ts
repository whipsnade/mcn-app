import { describe, expect, it } from "vitest";

import { startHealthServer, type GatewayMetricsSnapshot } from "../src/health.js";

function snapshot(overrides: Partial<GatewayMetricsSnapshot> = {}): GatewayMetricsSnapshot {
  return {
    uptime_seconds: 3,
    ready: true,
    draining: false,
    active_workers: 1,
    claims_total: 5,
    claims_empty: 2,
    claim_errors: 0,
    errors_total: 0,
    last_error_code: null,
    last_claim_at: "2026-08-10T00:00:00.000Z",
    event_delivery_failures_total: 0,
    event_delivery_retries_total: 0,
    event_buffer_overflows_total: 0,
    event_queue_high_water: 0,
    event_last_acked_source_sequence: null,
    ...overrides,
  };
}

describe("gateway health server", () => {
  it("serves healthz, readyz and metrics without secret material", async () => {
    const server = await startHealthServer({
      host: "127.0.0.1",
      port: 0,
      source: { snapshot: () => snapshot() },
    });
    try {
      const base = `http://127.0.0.1:${server.port}`;
      const health = await fetch(`${base}/healthz`);
      expect(health.status).toBe(200);
      expect(await health.json()).toEqual({ status: "ok" });

      const ready = await fetch(`${base}/readyz`);
      expect(ready.status).toBe(200);

      const metrics = await fetch(`${base}/metrics`);
      expect(metrics.status).toBe(200);
      const body = (await metrics.json()) as Record<string, unknown>;
      expect(body).toMatchObject({
        ready: true,
        draining: false,
        active_workers: 1,
        claims_total: 5,
      });
      expect(JSON.stringify(body)).not.toMatch(/secret|token|lease|key/i);

      const missing = await fetch(`${base}/admin`);
      expect(missing.status).toBe(404);
    } finally {
      await server.close();
    }
  });

  it("reports not-ready while draining", async () => {
    const server = await startHealthServer({
      host: "127.0.0.1",
      port: 0,
      source: { snapshot: () => snapshot({ draining: true, ready: false }) },
    });
    try {
      const ready = await fetch(`http://127.0.0.1:${server.port}/readyz`);
      expect(ready.status).toBe(503);
    } finally {
      await server.close();
    }
  });
});
