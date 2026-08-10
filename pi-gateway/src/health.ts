import { createServer, type Server } from "node:http";

/** Operational metrics projection; contains no tenant, user or secret data. */
export interface GatewayMetricsSnapshot {
  uptime_seconds: number;
  ready: boolean;
  draining: boolean;
  active_workers: number;
  claims_total: number;
  claims_empty: number;
  claim_errors: number;
  errors_total: number;
  last_error_code: string | null;
  last_claim_at: string | null;
}

export interface GatewayMetricsSource {
  snapshot(): GatewayMetricsSnapshot;
}

export interface GatewayHealthServer {
  readonly port: number;
  close(): Promise<void>;
}

function send(res: import("node:http").ServerResponse, status: number, body: unknown): void {
  const payload = JSON.stringify(body);
  res.writeHead(status, { "content-type": "application/json", "content-length": Buffer.byteLength(payload) });
  res.end(payload);
}

/**
 * Loopback-only operational HTTP surface: ``/healthz`` liveness, ``/readyz``
 * scheduling readiness and ``/metrics`` a bounded JSON counter snapshot.
 */
export async function startHealthServer(options: {
  host: string;
  port: number;
  source: GatewayMetricsSource;
}): Promise<GatewayHealthServer> {
  const server: Server = createServer((req, res) => {
    const path = (req.url ?? "").split("?", 1)[0];
    if (req.method !== "GET") {
      send(res, 404, { detail: "not_found" });
      return;
    }
    if (path === "/healthz") {
      send(res, 200, { status: "ok" });
      return;
    }
    if (path === "/readyz") {
      const snapshot = options.source.snapshot();
      if (snapshot.ready && !snapshot.draining) {
        send(res, 200, { status: "ready" });
      } else {
        send(res, 503, { status: "not_ready" });
      }
      return;
    }
    if (path === "/metrics") {
      send(res, 200, options.source.snapshot());
      return;
    }
    send(res, 404, { detail: "not_found" });
  });
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(options.port, options.host, () => {
      server.removeListener("error", reject);
      resolve();
    });
  });
  const address = server.address();
  if (!address || typeof address === "string") {
    server.close();
    throw new Error("pi_gateway_health_bind_failed");
  }
  return {
    port: address.port,
    close: () =>
      new Promise<void>((resolve, reject) => {
        server.closeAllConnections?.();
        server.close((error) => (error ? reject(error) : resolve()));
      }),
  };
}
