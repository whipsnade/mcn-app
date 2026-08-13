import { describe, expect, it } from "vitest";
import { access, mkdtemp, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";

import { classifyWorkerError, installWorkerSignalHandlers, runSingleWorker, spawnIsolatedWorker } from "../src/worker-entry.js";
import { ModelRequestBudget } from "../src/model-request-budget.js";
import type { ClaimedRun, PiRunSession, PiSessionFactory, SecretBundle } from "../src/protocol.js";

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
    maxDecisions: 50,
  },
};

const secrets: SecretBundle = {
  modelBaseUrl: "http://model.invalid",
  modelApiKey: "model-secret",
  datatapToken: "datatap-secret",
  datatapUrls: {
    "insight-cube": "http://127.0.0.1:2",
    "social-grow": "http://127.0.0.1:3",
    "social-grow-content": "http://127.0.0.1:4",
    aktools: "http://127.0.0.1:5",
  },
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
    // usage 去重后恰好一条（同一 provider 调用的 done/turn_end 不重复投影）；
    // 具体序号随投影顺序演进，不作硬编码。
    const usageEvents = messages.filter(
      (message) => message.type === "event" && (message.event as { event_type?: string })?.event_type === "usage",
    );
    expect(usageEvents).toHaveLength(1);
    expect(usageEvents[0]).toMatchObject({
      event: { event_type: "usage" },
    });
    expect(String((usageEvents[0] as { event: { source_event_id: string } }).event.source_event_id)).toMatch(/^attempt-worker:\d+$/);
    expect(projected.length).toBeGreaterThanOrEqual(4);
    expect(projected[0]).toMatchObject({ source_event_id: "attempt-worker:1" });
  });

  it("abort resolves only after the child closes, escalating to SIGKILL after the grace", async () => {
    const directory = await mkdtemp(join(tmpdir(), "worker-sigterm-ignore-"));
    const script = join(directory, "ignore-sigterm.mjs");
    await writeFile(script, [
      "process.on('SIGTERM', () => {});",
      "process.on('message', () => process.send({ type: 'ready' }));",
      "setInterval(() => {}, 1000);",
    ].join("\n"));
    const child = spawnIsolatedWorker(work, secrets, {
      workerScript: script,
      parentEnv: { PATH: "/usr/bin" },
      abortGraceMs: 50,
    });
    await new Promise<void>((resolve) => child.once("message", () => resolve()));
    let closed = false;
    child.once("close", () => { closed = true; });

    await child.abort();

    // abort 必须等 Child 真正 close（SIGTERM 被忽略 → 升级到 SIGKILL 后关闭）。
    expect(closed).toBe(true);
    expect(child.killed).toBe(true);
  });

  it("rejects done with the child terminal-frame business code, not a crash code", async () => {
    // 业务失败（worker_error）必须原样传给父进程，不得被记为可恢复的
    // worker_exited/worker_signaled 基础设施崩溃。
    const directory = await mkdtemp(join(tmpdir(), "worker-business-fail-"));
    const script = join(directory, "business-fail.mjs");
    await writeFile(script, [
      "process.on('message', () => {",
      "  process.send({ type: 'failed', runId: 'run-worker', errorCode: 'worker_error' }, () => {",
      "    process.disconnect();",
      "    process.exit(1);",
      "  });",
      "});",
    ].join("\n"));
    const child = spawnIsolatedWorker(work, secrets, {
      workerScript: script,
      parentEnv: { PATH: "/usr/bin" },
    });

    await expect(child.done).rejects.toMatchObject({ message: "worker_error" });
  });

  it("rejects done with worker_exited when the child dies without a terminal frame", async () => {
    const directory = await mkdtemp(join(tmpdir(), "worker-crash-noframe-"));
    const script = join(directory, "crash.mjs");
    await writeFile(script, [
      "process.on('message', () => process.exit(1));",
    ].join("\n"));
    const child = spawnIsolatedWorker(work, secrets, {
      workerScript: script,
      parentEnv: { PATH: "/usr/bin" },
    });

    await expect(child.done).rejects.toMatchObject({ message: "worker_exited" });
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

describe("pi_decision_limit stable failure classification", () => {
  it("classifyWorkerError preserves pi_decision_limit (message and code forms)", () => {
    expect(classifyWorkerError(new Error("pi_decision_limit"))).toBe("pi_decision_limit");
    const withCode = Object.assign(new Error("boom"), { code: "pi_decision_limit" });
    expect(classifyWorkerError(withCode)).toBe("pi_decision_limit");
    expect(classifyWorkerError(Object.assign(new Error("boom"), {
      code: "agent_loop_circuit_open",
    }))).toBe("agent_loop_circuit_open");
    expect(classifyWorkerError(new Error("sdk_protocol_error"))).toBe("sdk_protocol_error");
    expect(classifyWorkerError(new Error("something else"))).toBe("worker_error");
  });

  it("rejects done with pi_decision_limit from the child terminal frame", async () => {
    const directory = await mkdtemp(join(tmpdir(), "worker-decision-limit-"));
    const script = join(directory, "decision-limit.mjs");
    await writeFile(script, [
      "process.on('message', () => {",
      "  process.send({ type: 'failed', runId: 'run-worker', errorCode: 'pi_decision_limit' }, () => {",
      "    process.disconnect();",
      "    process.exit(1);",
      "  });",
      "});",
    ].join("\n"));
    const child = spawnIsolatedWorker(work, secrets, {
      workerScript: script,
      parentEnv: { PATH: "/usr/bin" },
    });

    await expect(child.done).rejects.toMatchObject({ message: "pi_decision_limit" });
  });

  it("runSingleWorker rethrows pi_decision_limit when the session budget was exceeded", async () => {
    const budget = new ModelRequestBudget(1);
    budget.assertAndConsume();
    expect(() => budget.assertAndConsume()).toThrow("pi_decision_limit");
    const sessionFactory: PiSessionFactory = {
      create: async () => ({
        prompt: async () => undefined,
        subscribe: () => () => undefined,
        abort: async () => undefined,
        dispose: async () => undefined,
        systemPrompt: () => "",
        activeToolNames: () => [],
        cwd: () => "/tmp",
        modelBudget: budget,
      }),
    };
    await expect(
      runSingleWorker(work, secrets, { sessionFactory }),
    ).rejects.toThrow("pi_decision_limit");
  });
});
