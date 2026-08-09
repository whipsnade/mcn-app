import { describe, expect, it } from "vitest";
import { access, mkdtemp, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";

import { installWorkerSignalHandlers, runSingleWorker, spawnIsolatedWorker } from "../src/worker-entry.js";
import type { ClaimedRun, PiRunSession, SecretBundle } from "../src/protocol.js";

const work: ClaimedRun = {
  runId: "run-worker",
  tenantId: "tenant-worker",
  userId: "user-worker",
  sessionId: "session-worker",
  attemptId: "attempt-worker",
  runtimeBackend: "pi",
  userPrompt: "question",
  runtimeSnapshot: {
    configVersionId: "config-worker",
    model: { provider: "fake", id: "model", api: "faux" },
    rootPolicy: "policy",
    skillCatalog: [],
    adapterCatalog: [
      { service: "insight-cube", adapterName: "cube", remoteName: "query", schemaDigest: "sha256:a" },
      { service: "social-grow", adapterName: "grow", remoteName: "query", schemaDigest: "sha256:b" },
      { service: "social-grow-content", adapterName: "content", remoteName: "query", schemaDigest: "sha256:c" },
      { service: "aktools", adapterName: "ak", remoteName: "query", schemaDigest: "sha256:d" },
    ],
  },
};

const secrets: SecretBundle = {
  modelBaseUrl: "http://model.invalid",
  modelApiKey: "model-secret",
  datatapToken: "datatap-secret",
  datatapUrls: {},
};

describe("single-run worker lifecycle", () => {
  it("projects SDK usage into a secret-free source event before the Gateway callback", async () => {
    const events: Array<Record<string, unknown>> = [];
    const session: PiRunSession = {
      prompt: async () => undefined,
      subscribe: (listener) => {
        listener({
          type: "sdk_event",
          eventType: "usage",
          event: { type: "usage", usage: { input: 4, requestId: "worker-request" } },
        });
        return () => undefined;
      },
      abort: async () => undefined,
      dispose: async () => undefined,
      systemPrompt: () => "policy",
      activeToolNames: () => [],
      cwd: () => "/tmp/worker",
    };
    await runSingleWorker(work, secrets, {
      sessionFactory: { create: async () => session },
      onEvent: (event) => events.push(event as unknown as Record<string, unknown>),
    });
    expect(events).toEqual([
      expect.objectContaining({
        source_event_id: "attempt-worker:1",
        event_type: "usage",
        payload: expect.objectContaining({ input_tokens: 4, upstream_request_id: "worker-request" }),
      }),
    ]);
  });

  it("orders abort, unsubscribe, dispose and clears child env on success", async () => {
    const order: string[] = [];
    const session: PiRunSession = {
      prompt: async () => { order.push("prompt"); },
      subscribe: () => {
        order.push("subscribe");
        return () => { order.push("unsubscribe"); };
      },
      abort: async () => { order.push("abort"); },
      dispose: async () => { order.push("dispose"); },
      systemPrompt: () => "policy",
      activeToolNames: () => [],
      cwd: () => "/tmp/worker",
    };
    await runSingleWorker(work, secrets, {
      parentEnv: { PATH: "/usr/bin", HOME: "/home/other" },
      sessionFactory: { create: async () => session },
    });
    expect(order).toEqual(["subscribe", "prompt", "abort", "unsubscribe", "dispose"]);
  });

  it("still cleans up when prompt rejects", async () => {
    const order: string[] = [];
    const session: PiRunSession = {
      prompt: async () => { order.push("prompt"); throw new Error("fake_failure"); },
      subscribe: () => () => { order.push("unsubscribe"); },
      abort: async () => { order.push("abort"); },
      dispose: async () => { order.push("dispose"); },
      systemPrompt: () => "policy",
      activeToolNames: () => [],
      cwd: () => "/tmp/worker",
    };
    await expect(runSingleWorker(work, secrets, { sessionFactory: { create: async () => session } })).rejects.toThrow("fake_failure");
    expect(order).toEqual(["prompt", "abort", "unsubscribe", "dispose"]);
  });

  it("disposes the session even when unsubscribe throws", async () => {
    const order: string[] = [];
    const session: PiRunSession = {
      prompt: async () => { order.push("prompt"); },
      subscribe: () => () => {
        order.push("unsubscribe");
        throw new Error("unsubscribe_failure");
      },
      abort: async () => { order.push("abort"); },
      dispose: async () => { order.push("dispose"); },
      systemPrompt: () => "policy",
      activeToolNames: () => [],
      cwd: () => "/tmp/worker",
    };
    await expect(runSingleWorker(work, secrets, { sessionFactory: { create: async () => session } }))
      .rejects.toThrow("unsubscribe_failure");
    expect(order).toEqual(["prompt", "abort", "unsubscribe", "dispose"]);
  });

  it("never removes a caller-owned path named like a worker temp variable", async () => {
    const callerDirectory = await mkdtemp(join(tmpdir(), "caller-owned-"));
    const callerFile = join(callerDirectory, "keep.txt");
    await writeFile(callerFile, "must survive");
    const session: PiRunSession = {
      prompt: async () => undefined,
      subscribe: () => () => undefined,
      abort: async () => undefined,
      dispose: async () => undefined,
      systemPrompt: () => "policy",
      activeToolNames: () => [],
      cwd: () => "/tmp/worker",
    };
    await runSingleWorker(work, secrets, {
      parentEnv: { PI_WORKER_TEMP_DIR: callerDirectory },
      sessionFactory: { create: async () => session },
    });
    await expect(access(callerFile)).resolves.toBeUndefined();
  });

  it("sends only non-secret work over IPC and keeps secrets in child env", async () => {
    const directory = await mkdtemp(join(tmpdir(), "worker-probe-"));
    const script = join(directory, "probe.mjs");
    await writeFile(script, [
      "process.on('message', (message) => {",
      "  process.send({ work: message.work, hasModelSecret: Boolean(process.env.PI_MODEL_API_KEY), hasDatatapSecret: Boolean(process.env.PI_DATATAP_TOKEN), payloadHasSecret: JSON.stringify(message).includes('secret') });",
      "  process.exit(0);",
      "});",
    ].join("\n"));
    const child = spawnIsolatedWorker(work, secrets, { workerScript: script, parentEnv: { PATH: "/usr/bin" } });
    const result = await new Promise<Record<string, unknown>>((resolve) => child.once("message", resolve));
    expect(result).toMatchObject({ work: { runId: work.runId }, hasModelSecret: true, hasDatatapSecret: true, payloadHasSecret: false });
  });

  it("disconnects the real worker child after one fake run", async () => {
    const workerScript = fileURLToPath(new URL("../src/worker-entry.ts", import.meta.url));
    const child = spawnIsolatedWorker(work, secrets, {
      workerScript,
      execArgv: ["--import", "tsx"],
      parentEnv: { PATH: process.env.PATH ?? "/usr/bin" },
    });
    const messages: Array<Record<string, unknown>> = [];
    const projected: Array<Record<string, unknown>> = [];
    child.onEvent((event) => projected.push(event as unknown as Record<string, unknown>));
    child.on("message", (message: Record<string, unknown>) => messages.push(message));
    await new Promise<void>((resolve, reject) => {
      child.once("error", reject);
      child.once("close", () => resolve());
    });
    expect(messages[0]).toEqual({ type: "ready", runId: work.runId });
    expect(messages.at(-1)).toEqual({ type: "done", runId: work.runId });
    expect(messages.filter((message) => message.type === "event").length).toBeGreaterThanOrEqual(4);
    expect(messages.find((message) => message.type === "event" && (message.event as { event_type?: string })?.event_type === "usage")).toMatchObject({
      event: { source_event_id: "attempt-worker:4", event_type: "usage" },
    });
    expect(projected.length).toBeGreaterThanOrEqual(4);
    expect(projected[0]).toMatchObject({ source_event_id: "attempt-worker:1" });
  });

  it("installs a signal cleanup hook exactly once", async () => {
    let calls = 0;
    const remove = installWorkerSignalHandlers(async () => { calls += 1; });
    process.emit("SIGTERM");
    await new Promise((resolve) => setImmediate(resolve));
    remove();
    expect(calls).toBe(1);
  });
});
