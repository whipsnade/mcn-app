import { describe, expect, it } from "vitest";
import { access, stat } from "node:fs/promises";

import { createProductionPiSession } from "../src/pi-session.js";
import type { ClaimedRun, PiSdkEvent, SecretBundle, SkillSnapshotEntry } from "../src/protocol.js";

const work: ClaimedRun = {
  runId: "run-a",
  tenantId: "tenant-a",
  userId: "user-a",
  sessionId: "session-a",
  attemptId: "attempt-a",
  runtimeBackend: "pi",
  runtimeSnapshot: {
    configVersionId: "tenant-a-v1",
    model: { provider: "fake", id: "fake-model", api: "openai-completions" },
    rootPolicy: "ROOT POLICY: do not disclose secrets",
    skillCatalog: [],
    allowedArtifactContracts: [],
    adapterCatalog: [
      { service: "insight-cube", adapterName: "mcp__insight_cube", remoteName: "cube", schemaDigest: "sha256:a" },
      { service: "social-grow", adapterName: "mcp__social_grow", remoteName: "grow", schemaDigest: "sha256:b" },
      { service: "social-grow-content", adapterName: "mcp__social_grow_content", remoteName: "content", schemaDigest: "sha256:c" },
      { service: "aktools", adapterName: "mcp__aktools", remoteName: "ak", schemaDigest: "sha256:d" },
    ],
    maxDecisions: 50,
  },
};

const secrets: SecretBundle = {
  modelBaseUrl: "http://127.0.0.1:1",
  modelApiKey: "fake-model-key",
  datatapToken: "fake-datatap-token",
  datatapUrls: {
    insightCube: "http://127.0.0.1:2",
    socialGrow: "http://127.0.0.1:3",
    socialGrowContent: "http://127.0.0.1:4",
    aktools: "http://127.0.0.1:5",
  },
};

describe("Pi SDK session factory", () => {
  it("rejects an invalid Skill snapshot before readiness, model, or MCP setup", async () => {
    const invalidEntry: SkillSnapshotEntry = {
      name: "campaign-research",
      revision: 1,
      contentDigest: "0".repeat(64),
      description: "tampered",
      requiredTools: [],
      artifactContract: null,
      content: "tampered",
    };
    await expect(createProductionPiSession({
      ...work,
      runtimeSnapshot: {
        ...work.runtimeSnapshot,
        skillManifest: {
          entries: [invalidEntry],
          manifestDigest: "0".repeat(64),
          sourceScope: "database_activation",
        },
      },
    }, secrets, {
      fakeProvider: true,
      mcpReadiness: { waitUntilReady: async () => { throw new Error("must_not_start"); } },
    } as any)).rejects.toThrow("pi_skill_snapshot_digest_mismatch");
  });

  it("does not resolve or allow prompt before MCP readiness completes", async () => {
    let release!: () => void;
    let signalStarted!: () => void;
    const started = new Promise<void>((resolve) => { signalStarted = resolve; });
    const readiness = {
      waitUntilReady: () => new Promise<void>((resolve) => {
        release = resolve;
        signalStarted();
      }),
    };
    let created = false;
    const creating = createProductionPiSession(work, secrets, {
      fakeProvider: true,
      mcpReadiness: readiness,
    } as any).then((session) => {
      created = true;
      return session;
    });
    await started;
    expect(created).toBe(false);
    release();
    const session = await creating;
    await session.prompt("first prompt after readiness");
    await session.dispose();
  });

  it("fails closed before session creation when MCP readiness fails", async () => {
    await expect(createProductionPiSession(work, secrets, {
      fakeProvider: true,
      mcpReadiness: { waitUntilReady: async () => { throw new Error("pi_mcp_readiness_failed"); } },
    } as any)).rejects.toThrow("pi_mcp_readiness_failed");
  });

  it("creates an in-memory session with the root policy in system prompt", async () => {
    const session = await createProductionPiSession(work, secrets, { fakeProvider: true });
    const events: PiSdkEvent[] = [];
    const unsubscribe = session.subscribe((event) => events.push(event));

    expect(session.systemPrompt()).toContain(work.runtimeSnapshot.rootPolicy);
    expect(session.activeToolNames()).not.toEqual(expect.arrayContaining(["read", "bash", "edit", "write", "grep", "find", "ls"]));
    await session.prompt("user question only");
    expect(events.some((event) => event.type === "user_prompt" && event.content === "user question only")).toBe(true);
    expect(events.some((event) => event.type === "user_prompt" && event.content.includes(work.runtimeSnapshot.rootPolicy))).toBe(false);

    unsubscribe();
    await session.abort();
    await session.dispose();
    await expect(access(session.cwd())).rejects.toThrow();
  });

  it("keeps two tenants' session state and temporary directories isolated", async () => {
    const other = await createProductionPiSession({ ...work, runId: "run-b", tenantId: "tenant-b" }, secrets, { fakeProvider: true });
    const first = await createProductionPiSession(work, secrets, { fakeProvider: true });
    expect(first.cwd()).not.toBe(other.cwd());
    expect(first.systemPrompt()).not.toContain(other.cwd());
    expect(other.systemPrompt()).not.toContain(first.cwd());
    expect(first.cwd()).not.toContain("/Users");
    const directoryMode = (await stat(first.cwd())).mode & 0o777;
    expect(directoryMode).toBe(0o700);
    await first.dispose();
    await other.dispose();
  });

  it("exposes the MCP accounting hook without placing billing data in the SDK prompt", async () => {
    const control = {
      preflight: async () => ({ permit_id: "permit-1" }),
      finalize: async () => undefined,
      fail: async () => undefined,
    };
    const session = await createProductionPiSession(work, secrets, {
      fakeProvider: true,
      mcpAccounting: control,
    });
    expect(session.mcpAccounting).toBeDefined();
    expect(await session.mcpAccounting?.beforeToolCall({
      tool: "query_analysis_data",
      server: "insight-cube-mcp",
      args: { keyword: "美妆" },
    })).toEqual({ permit_id: "permit-1" });
    expect(session.systemPrompt()).not.toContain("permit-1");
    await session.dispose();
  });

  it("disables both SDK auto-retry layers in the actual session settings", async () => {
    const session = await createProductionPiSession(work, secrets, { fakeProvider: true });
    const retry = session.retrySettings?.();
    expect(retry).toBeDefined();
    expect(retry?.agent.enabled).toBe(false);
    expect(retry?.provider.maxRetries).toBe(0);
    await session.dispose();
  });

  it("enforces the snapshot maxDecisions budget at the provider boundary", async () => {
    // maxDecisions=1：第一次决策（文本）放行；若 SDK 再次请求模型，第二次
    // 在任何外发前被 pi_decision_limit 拦截（此处用单步脚本验证预算门接线）。
    const budgeted = {
      ...work,
      runtimeSnapshot: { ...work.runtimeSnapshot, maxDecisions: 1 },
    };
    const session = await createProductionPiSession(budgeted, secrets, {
      fakeProvider: true,
      fakeScript: [{ kind: "text", text: "一次回答" }],
    });
    expect(session.modelBudget?.maxDecisions).toBe(1);
    expect(session.modelBudget?.usedCount).toBe(0);
    await session.prompt("问一个问题");
    expect(session.modelBudget?.usedCount).toBe(1);
    expect(session.modelBudget?.limitExceeded).toBe(false);
    await session.dispose();
  });
});
