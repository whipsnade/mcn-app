import { describe, expect, it } from "vitest";
import { access, stat } from "node:fs/promises";

import { createProductionPiSession } from "../src/pi-session.js";
import type { ClaimedRun, PiSdkEvent, SecretBundle } from "../src/protocol.js";

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
    adapterCatalog: [
      { service: "insight-cube", adapterName: "mcp__insight_cube", remoteName: "cube", schemaDigest: "sha256:a" },
      { service: "social-grow", adapterName: "mcp__social_grow", remoteName: "grow", schemaDigest: "sha256:b" },
      { service: "social-grow-content", adapterName: "mcp__social_grow_content", remoteName: "content", schemaDigest: "sha256:c" },
      { service: "aktools", adapterName: "mcp__aktools", remoteName: "ak", schemaDigest: "sha256:d" },
    ],
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
});
