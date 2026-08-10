import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { attachWorkerRpcBridge, type WorkerRpcHandlers } from "../src/worker-bridge.js";
import { spawnIsolatedWorker } from "../src/worker-entry.js";
import type { ClaimedRun, SecretBundle } from "../src/protocol.js";

const ADAPTER_CATALOG = [
  { service: "insight-cube", adapterName: "cube", remoteName: "query", schemaDigest: "sha256:a" },
  { service: "social-grow", adapterName: "grow", remoteName: "query", schemaDigest: "sha256:b" },
  { service: "social-grow-content", adapterName: "content", remoteName: "query", schemaDigest: "sha256:c" },
  { service: "aktools", adapterName: "ak", remoteName: "query", schemaDigest: "sha256:d" },
];

const SECRETS: SecretBundle = {
  modelBaseUrl: "http://127.0.0.1:1",
  modelApiKey: "bridge-model-secret",
  datatapToken: "bridge-datatap-secret",
  datatapUrls: {
    "insight-cube": "http://127.0.0.1:2",
    "social-grow": "http://127.0.0.1:3",
    "social-grow-content": "http://127.0.0.1:4",
    aktools: "http://127.0.0.1:5",
  },
};

function makeWork(overrides: Partial<ClaimedRun> = {}): ClaimedRun {
  return {
    runId: "run-bridge",
    attemptId: "attempt-bridge",
    runtimeBackend: "pi",
    runtimeSnapshot: {
      configVersionId: "config-bridge",
      model: { provider: "fake", id: "fake-model", api: "faux" },
      rootPolicy: "policy",
      skillCatalog: [],
      adapterCatalog: ADAPTER_CATALOG,
    },
    userPrompt: "分析这个品牌",
    ...overrides,
  };
}

function recordingHandlers(
  calls: Array<{ method: string; params: Record<string, unknown> }>,
  overrides: Partial<WorkerRpcHandlers> = {},
): WorkerRpcHandlers {
  const record =
    (method: string, result: unknown) =>
    async (params: Record<string, unknown>) => {
      calls.push({ method, params });
      return result;
    };
  return {
    internal_tool: record("internal_tool", { context: { summary: "受限会话上下文" } }),
    mcp_preflight: record("mcp_preflight", { permit_id: "permit-1", catalog_entry_id: "ce-1" }),
    mcp_finalize: record("mcp_finalize", { status: "settled" }),
    mcp_fail: record("mcp_fail", { ok: true }),
    ...overrides,
  };
}

describe("isolated worker control-plane bridge", () => {
  let workerScript = "";
  let tempdirs: string[] = [];

  beforeAll(async () => {
    workerScript = fileURLToPath(new URL("../src/worker-entry.ts", import.meta.url));
  });

  afterAll(async () => {
    for (const dir of tempdirs) await rm(dir, { recursive: true, force: true });
  });

  it("executes an SDK internal tool call through the real child IPC bridge", async () => {
    const work = makeWork({
      internalTools: ["get_session_context", "read_artifact"],
      fakeScript: [
        { kind: "tool_call", tool: "get_session_context", args: {} },
        { kind: "text", text: "分析完成" },
      ],
    });
    const child = spawnIsolatedWorker(work, SECRETS, {
      workerScript,
      execArgv: ["--import", "tsx"],
      parentEnv: { PATH: process.env.PATH ?? "/usr/bin" },
    });
    const baselineListeners = child.listenerCount("message");
    const calls: Array<{ method: string; params: Record<string, unknown> }> = [];
    attachWorkerRpcBridge(child, recordingHandlers(calls));
    expect(child.listenerCount("message")).toBe(baselineListeners + 1);
    const events: string[] = [];
    child.onEvent((event) => events.push(event.event_type));

    await child.done;

    expect(calls).toEqual([
      { method: "internal_tool", params: { tool_name: "get_session_context", args: {} } },
    ]);
    expect(events).toContain("tool.started");
    expect(events).toContain("tool.succeeded");
    // bridge listener detached after child exit
    expect(child.listenerCount("message")).toBe(baselineListeners);
  }, 60_000);

  it("never forwards a tool the claim did not allow", async () => {
    const work = makeWork({
      internalTools: ["get_session_context"],
      fakeScript: [
        { kind: "tool_call", tool: "publish_artifacts", args: { draft_ids: ["d-1"] } },
        { kind: "text", text: "结束" },
      ],
    });
    const child = spawnIsolatedWorker(work, SECRETS, {
      workerScript,
      execArgv: ["--import", "tsx"],
      parentEnv: { PATH: process.env.PATH ?? "/usr/bin" },
    });
    const calls: Array<{ method: string; params: Record<string, unknown> }> = [];
    attachWorkerRpcBridge(child, recordingHandlers(calls));
    await child.done;
    expect(calls).toEqual([]);
  }, 60_000);

  it("blocks the MCP call when preflight rejects and never finalizes", async () => {
    const work = makeWork({
      internalTools: [],
      fakeScript: [
        {
          kind: "tool_call",
          tool: "mcp",
          args: { tool: "query_analysis_data", server: "insight-cube", args: { keyword: "美妆" } },
        },
        { kind: "text", text: "无法调用数据工具" },
      ],
    });
    const child = spawnIsolatedWorker(work, SECRETS, {
      workerScript,
      execArgv: ["--import", "tsx"],
      parentEnv: { PATH: process.env.PATH ?? "/usr/bin" },
    });
    const calls: Array<{ method: string; params: Record<string, unknown> }> = [];
    attachWorkerRpcBridge(
      child,
      recordingHandlers(calls, {
        mcp_preflight: async () => {
          calls.push({ method: "mcp_preflight", params: {} });
          throw new Error("insufficient_points");
        },
      }),
    );
    await child.done;
    const methods = calls.map((call) => call.method);
    expect(methods).toContain("mcp_preflight");
    expect(methods).not.toContain("mcp_finalize");
    expect(methods).not.toContain("mcp_fail");
  }, 60_000);

  it("rejects malformed IPC requests without crashing the parent bridge", async () => {
    const dir = await mkdtemp(join(tmpdir(), "rpc-probe-"));
    tempdirs.push(dir);
    const probe = join(dir, "probe.mjs");
    await writeFile(probe, [
      "process.on('message', (message) => {",
      "  if (!message || message.type !== 'run') return;",
      "  process.send({ type: 'worker_rpc', id: 'bad-1', method: 'shell', params: {} });",
      "  process.send({ type: 'worker_rpc', id: 'ok-1', method: 'internal_tool', params: { tool_name: 'read_artifact', args: {} } });",
      "  process.on('message', (nested) => {",
      "    if (!nested || nested.type !== 'worker_rpc_result') return;",
      "    process.send({ type: 'event', runId: message.work.runId, event: {",
      "      source_event_id: message.work.attemptId + ':1',",
      "      sequence: 1,",
      "      event_type: 'message.completed',",
      "      payload: { text: nested.id + ':' + nested.ok },",
      "    } });",
      "    if (nested.id === 'ok-1') {",
      "      process.send({ type: 'done', runId: message.work.runId });",
      "      process.disconnect();",
      "    }",
      "  });",
      "});",
    ].join("\n"));
    const child = spawnIsolatedWorker(makeWork(), SECRETS, {
      workerScript: probe,
      parentEnv: { PATH: process.env.PATH ?? "/usr/bin" },
    });
    const calls: Array<{ method: string; params: Record<string, unknown> }> = [];
    attachWorkerRpcBridge(child, recordingHandlers(calls));
    const events: string[] = [];
    child.onEvent((event) => {
      if (typeof event.payload.text === "string") events.push(event.payload.text);
    });
    await child.done;
    expect(events).toContain("bad-1:false");
    expect(events).toContain("ok-1:true");
    expect(calls).toEqual([
      { method: "internal_tool", params: { tool_name: "read_artifact", args: {} } },
    ]);
  }, 60_000);
});
