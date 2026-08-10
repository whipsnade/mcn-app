import { createCipheriv, hkdfSync, randomBytes } from "node:crypto";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createServer, type IncomingMessage, type Server } from "node:http";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { buildSignature } from "../src/control-plane-client.js";
import { runGatewayMain } from "../src/main.js";

const GATEWAY_SECRET = "test-only-gateway-secret-0123456789";
const LEASE_TOKEN = "lease-token-with-enough-entropy-0123456789";
const MODEL_SECRET = "model-secret-probe";
const DATATAP_SECRET = "datatap-secret-probe";
const CONFIG_VERSION_ID = "config-main-1";
const GATEWAY_ID = "gw-main-test";

interface RecordedRequest {
  method: string;
  path: string;
  signatureValid: boolean;
  body: Record<string, unknown>;
}

function sealForTest(
  bundle: Record<string, unknown>,
  binding: { runId: string; attemptId: string; configVersionId: string; gatewayId: string },
): { alg: "AES-256-GCM"; nonce: string; ciphertext: string } {
  const aad = Buffer.from(
    `${binding.runId}:${binding.attemptId}:${binding.configVersionId}:${binding.gatewayId}`,
  );
  const key = Buffer.from(hkdfSync(
    "sha256",
    Buffer.from(LEASE_TOKEN),
    Buffer.from("pi-gateway-secret-v1"),
    Buffer.from(`lease:${aad.toString()}`),
    32,
  ));
  const nonce = randomBytes(12);
  const cipher = createCipheriv("aes-256-gcm", key, nonce);
  cipher.setAAD(aad);
  const ciphertext = Buffer.concat([cipher.update(JSON.stringify(bundle), "utf8"), cipher.final()]);
  return {
    alg: "AES-256-GCM",
    nonce: nonce.toString("base64"),
    ciphertext: Buffer.concat([ciphertext, cipher.getAuthTag()]).toString("base64"),
  };
}

function shaDigest(suffix: string): string {
  return `sha256:${suffix.repeat(64).slice(0, 64)}`;
}

function claimResponse(overrides: { tamperAad?: boolean } = {}) {
  const binding = {
    runId: overrides.tamperAad ? "run-other" : "run-main-1",
    attemptId: "attempt-main-1",
    configVersionId: CONFIG_VERSION_ID,
    gatewayId: GATEWAY_ID,
  };
  return {
    run_id: "run-main-1",
    attempt_id: "attempt-main-1",
    lease_token: LEASE_TOKEN,
    runtime_snapshot: {
      config_version_id: CONFIG_VERSION_ID,
      runtime_contract_version: "marketing_runtime_v1",
      runtime_backend: "pi",
      model: { name: "fake-model", masked_origin: "http://127.0.0.1", provider: "fake" },
      datatap: { service: "datatap", schema_digest: shaDigest("ab") },
      capability_pack: {
        pack_name: "marketing",
        pack_version: "1.0.0",
        root_policy: "probe root policy",
        skills: [{
          name: "brand-research-report",
          version: "1.0.0",
          digest: shaDigest("cd"),
          content: "description: 品牌研究报告\nbody",
          artifact_contract: "brand_report_v3",
        }],
      },
      limits: { max_decisions: 50 },
      billing: { mcp_call_points: 10 },
    },
    transcript: [{ role: "user", content: "分析这个品牌" }],
    secret_envelope: sealForTest({
      model_base_url: "http://127.0.0.1:9",
      model_api_key: MODEL_SECRET,
      datatap_token: DATATAP_SECRET,
      datatap_urls: {},
    }, binding),
    adapter_catalog: [
      { catalog_entry_id: "ce-1", adapter_visible_name: "cube_query", service: "insight-cube-mcp", remote_name: "query", input_schema_digest: shaDigest("1") },
      { catalog_entry_id: "ce-2", adapter_visible_name: "grow_query", service: "social-grow-mcp", remote_name: "query", input_schema_digest: shaDigest("2") },
      { catalog_entry_id: "ce-3", adapter_visible_name: "content_query", service: "social-grow-content-mcp", remote_name: "query", input_schema_digest: shaDigest("3") },
      { catalog_entry_id: "ce-4", adapter_visible_name: "ak_query", service: "aktools-mcp", remote_name: "query", input_schema_digest: shaDigest("4") },
    ],
    internal_tools: [{ name: "get_session_context" }],
  };
}

interface FakeControlPlane {
  url: string;
  requests: RecordedRequest[];
  close(): Promise<void>;
}

async function startFakeControlPlane(options: {
  failClaims?: number;
  tamperAad?: boolean;
} = {}): Promise<FakeControlPlane> {
  const requests: RecordedRequest[] = [];
  let claimCount = 0;
  let failedClaims = 0;
  const server: Server = createServer((req: IncomingMessage, res) => {
    const chunks: Buffer[] = [];
    req.on("data", (chunk: Buffer) => chunks.push(chunk));
    req.on("end", () => {
      const body = Buffer.concat(chunks).toString("utf8");
      const path = req.url ?? "";
      const method = req.method ?? "";
      const expected = buildSignature(
        GATEWAY_SECRET,
        method,
        path,
        Number(req.headers["x-pi-timestamp"]),
        String(req.headers["x-pi-nonce"] ?? ""),
        body,
      );
      const signatureValid = expected === String(req.headers["x-pi-signature"] ?? "");
      let parsed: Record<string, unknown> = {};
      try {
        parsed = body ? (JSON.parse(body) as Record<string, unknown>) : {};
      } catch {
        parsed = { unparseable: true };
      }
      // The idle claim loop can issue unbounded requests in fast tests; the
      // assertions below only need the leading protocol window.
      if (requests.length < 500) {
        requests.push({ method, path, signatureValid, body: parsed });
      }
      if (!signatureValid || req.headers["x-pi-gateway-id"] !== GATEWAY_ID) {
        res.writeHead(401, { "content-type": "application/json" });
        res.end(JSON.stringify({ detail: "pi_gateway_auth_failed" }));
        return;
      }
      if (path === "/api/v1/internal/pi-gateway/v1/claims") {
        claimCount += 1;
        if (failedClaims < (options.failClaims ?? 0)) {
          failedClaims += 1;
          res.writeHead(500, { "content-type": "application/json" });
          res.end(JSON.stringify({ detail: "boom" }));
          return;
        }
        if (claimCount - failedClaims > 1) {
          res.writeHead(204);
          res.end();
          return;
        }
        res.writeHead(200, { "content-type": "application/json" });
        res.end(JSON.stringify(claimResponse({ tamperAad: options.tamperAad })));
        return;
      }
      if (path.endsWith("/heartbeat")) {
        res.writeHead(200, { "content-type": "application/json" });
        res.end(JSON.stringify({ ok: true, cancel_requested: false }));
        return;
      }
      if (path.endsWith("/events")) {
        res.writeHead(200, { "content-type": "application/json" });
        res.end(JSON.stringify({ event_id: "evt-1", sequence: 1, duplicate: false }));
        return;
      }
      if (path.endsWith("/terminal")) {
        res.writeHead(200, { "content-type": "application/json" });
        res.end(JSON.stringify({ status: "completed" }));
        return;
      }
      res.writeHead(404, { "content-type": "application/json" });
      res.end(JSON.stringify({ detail: "not_found" }));
    });
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("fake_control_plane_bind_failed");
  return {
    url: `http://127.0.0.1:${address.port}`,
    requests,
    close: () => new Promise<void>((resolve, reject) => {
      server.close((error) => (error ? reject(error) : resolve()));
      server.closeAllConnections?.();
    }),
  };
}

function signalHarness() {
  const listeners = new Map<string, () => void>();
  return {
    source: {
      on(event: "SIGTERM" | "SIGINT", listener: () => void) {
        listeners.set(event, listener);
        return this;
      },
      removeListener(event: "SIGTERM" | "SIGINT") {
        listeners.delete(event);
        return this;
      },
    },
    emit(event: "SIGTERM" | "SIGINT") {
      listeners.get(event)?.();
    },
  };
}

function baseEnv(controlPlaneUrl: string, workerScript: string, healthPort = 0): NodeJS.ProcessEnv {
  return {
    PI_GATEWAY_ID: GATEWAY_ID,
    PI_GATEWAY_CONTROL_PLANE_URL: controlPlaneUrl,
    PI_GATEWAY_INTERNAL_SECRET: GATEWAY_SECRET,
    PI_GATEWAY_ENVIRONMENT: "test",
    PI_GATEWAY_HEALTH_PORT: String(healthPort),
    PI_GATEWAY_CLAIM_INTERVAL_MS: "25",
    PI_GATEWAY_CLAIM_MAX_BACKOFF_MS: "400",
    PI_GATEWAY_HEARTBEAT_INTERVAL_MS: "20",
    PI_GATEWAY_SHUTDOWN_TIMEOUT_MS: "2000",
    PI_GATEWAY_WORKER_SCRIPT: workerScript,
  };
}

describe("production gateway composition root", () => {
  let probeScript = "";
  let workdir = "";
  const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

  beforeAll(async () => {
    workdir = await mkdtemp(join(tmpdir(), "pi-gateway-main-test-"));
    probeScript = join(workdir, "probe-worker.mjs");
    await writeFile(probeScript, [
      "process.on('message', (message) => {",
      "  if (!message || message.type !== 'run') return;",
      "  const hasSecrets = Boolean(process.env.PI_MODEL_API_KEY && process.env.PI_DATATAP_TOKEN);",
      "  const ipcHasSecret = JSON.stringify(message).includes('secret-probe');",
      "  process.send({ type: 'ready', runId: message.work.runId });",
      "  process.send({ type: 'event', runId: message.work.runId, event: {",
      "    source_event_id: message.work.attemptId + ':1',",
      "    sequence: 1,",
      "    event_type: 'message.completed',",
      "    payload: { text: 'secrets:' + hasSecrets + ':ipc:' + ipcHasSecret },",
      "  } });",
      "  process.send({ type: 'done', runId: message.work.runId });",
      "  process.disconnect();",
      "});",
    ].join("\n"), { mode: 0o700 });
  });

  afterAll(async () => {
    const { rm } = await import("node:fs/promises");
    await rm(workdir, { recursive: true, force: true });
  });

  it("runs claim -> decrypt -> isolated child -> event -> terminal over signed HTTP", async () => {
    const controlPlane = await startFakeControlPlane();
    const signals = signalHarness();
    const logs: string[] = [];
    const mainPromise = runGatewayMain({
      env: baseEnv(controlPlane.url, probeScript),
      signalSource: signals.source,
      logger: (line) => logs.push(line),
      random: () => 0.5,
    });
    try {
      const deadline = Date.now() + 10_000;
      while (Date.now() < deadline) {
        const terminal = controlPlane.requests.find((request) => request.path.endsWith("/terminal"));
        if (terminal) break;
        await sleep(25);
      }
      signals.emit("SIGTERM");
      await expect(mainPromise).resolves.toBe(0);

      const paths = controlPlane.requests.map((request) => request.path);
      expect(paths[0]).toBe("/api/v1/internal/pi-gateway/v1/claims");
      expect(paths.some((path) => path.endsWith("/heartbeat"))).toBe(true);
      expect(controlPlane.requests.every((request) => request.signatureValid)).toBe(true);
      expect(controlPlane.requests.every((request) => request.method === "POST")).toBe(true);

      const event = controlPlane.requests.find((request) => request.path.endsWith("/events"));
      expect(event).toBeDefined();
      // secrets:true -> child env carried the decrypted bundle;
      // ipc:false -> the IPC run payload never contains secret material.
      expect(JSON.stringify(event?.body)).toContain("secrets:true:ipc:false");
      expect(JSON.stringify(event?.body)).not.toContain(MODEL_SECRET);
      expect(JSON.stringify(event?.body)).not.toContain(DATATAP_SECRET);

      const terminal = controlPlane.requests.find((request) => request.path.endsWith("/terminal"));
      expect(terminal?.body).toMatchObject({ attempt_id: "attempt-main-1", outcome: "completed" });
      const eventIndex = paths.findIndex((path) => path.endsWith("/events"));
      const terminalIndex = paths.findIndex((path) => path.endsWith("/terminal"));
      expect(eventIndex).toBeGreaterThan(-1);
      expect(terminalIndex).toBeGreaterThan(eventIndex);

      // no secret ever reaches the wire or the log sink
      const wire = JSON.stringify(controlPlane.requests);
      expect(wire).not.toContain(MODEL_SECRET);
      expect(wire).not.toContain(DATATAP_SECRET);
      expect(logs.join("\n")).not.toContain(MODEL_SECRET);
      expect(logs.join("\n")).not.toContain(GATEWAY_SECRET);
    } finally {
      await controlPlane.close();
    }
  }, 20_000);

  it("backs off with a bounded delay while the control plane is failing", async () => {
    const controlPlane = await startFakeControlPlane({ failClaims: 3 });
    const signals = signalHarness();
    const sleeps: number[] = [];
    const mainPromise = runGatewayMain({
      env: baseEnv(controlPlane.url, probeScript),
      signalSource: signals.source,
      sleep: async (ms) => {
        sleeps.push(ms);
      },
      random: () => 0.5,
    });
    try {
      const deadline = Date.now() + 10_000;
      while (Date.now() < deadline && sleeps.length < 6) {
        await sleep(10);
      }
      signals.emit("SIGINT");
      await expect(mainPromise).resolves.toBe(0);
      expect(sleeps.length).toBeGreaterThanOrEqual(4);
      const backoff = sleeps.slice(0, 3);
      expect(backoff[0]).toBeGreaterThan(25);
      expect(backoff[1]).toBeGreaterThan(backoff[0]);
      expect(backoff[2]).toBeGreaterThan(backoff[1]);
      // The only sleep allowed above the claim backoff ceiling is the
      // separate 2000ms shutdown bound from PI_GATEWAY_SHUTDOWN_TIMEOUT_MS.
      const claimSleeps = sleeps.filter((ms) => ms <= 400);
      expect(Math.max(...claimSleeps)).toBeLessThanOrEqual(400);
      expect(sleeps.filter((ms) => ms > 400)).toEqual([2000]);
    } finally {
      await controlPlane.close();
    }
  }, 20_000);

  it("fails the claimed run without leaking secrets when the envelope AAD does not match", async () => {
    const controlPlane = await startFakeControlPlane({ tamperAad: true });
    const signals = signalHarness();
    const logs: string[] = [];
    const mainPromise = runGatewayMain({
      env: baseEnv(controlPlane.url, probeScript),
      signalSource: signals.source,
      logger: (line) => logs.push(line),
      random: () => 0.5,
    });
    try {
      const deadline = Date.now() + 10_000;
      while (Date.now() < deadline) {
        const terminal = controlPlane.requests.find((request) => request.path.endsWith("/terminal"));
        if (terminal) break;
        await sleep(25);
      }
      signals.emit("SIGTERM");
      await expect(mainPromise).resolves.toBe(0);
      const terminal = controlPlane.requests.find((request) => request.path.endsWith("/terminal"));
      expect(terminal?.body).toMatchObject({ outcome: "failed" });
      const wire = JSON.stringify(controlPlane.requests);
      expect(wire).not.toContain(MODEL_SECRET);
      expect(wire).not.toContain(DATATAP_SECRET);
      expect(logs.join("\n")).not.toContain(MODEL_SECRET);
    } finally {
      await controlPlane.close();
    }
  }, 20_000);

  it("exits 1 without starting the loop when configuration is invalid", async () => {
    const logs: string[] = [];
    await expect(runGatewayMain({
      env: { PI_GATEWAY_ID: "gw-only" },
      signalSource: signalHarness().source,
      logger: (line) => logs.push(line),
    })).resolves.toBe(1);
    expect(logs.join("\n")).toContain("PI_GATEWAY_CONTROL_PLANE_URL");
  });

  it("keeps the gateway free of database drivers", async () => {
    const raw = await readFile(new URL("../package.json", import.meta.url), "utf8");
    const pkg = JSON.parse(raw) as { dependencies?: Record<string, string>; devDependencies?: Record<string, string> };
    const deps = Object.keys({ ...pkg.dependencies, ...pkg.devDependencies });
    expect(deps.filter((dep) => /mysql|mariadb|typeorm|sequelize|knex|mongodb|sqlite|better-sqlite|pg($|[-_])/i.test(dep))).toEqual([]);
  });
});
