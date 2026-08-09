import { describe, expect, it } from "vitest";

import {
  PI_DEPENDENCY_VERSIONS,
  type ClaimedRun,
  type PiSdkEvent,
  type PiRunSession,
  type SecretBundle,
  assertPiSdkContract,
} from "../src/protocol.js";

describe("locked Pi SDK contract", () => {
  it("exposes the locked production dependency tuple", () => {
    expect(PI_DEPENDENCY_VERSIONS).toEqual({
      codingAgent: "0.79.10",
      piAi: "0.74.2",
      piTui: "0.74.2",
      mcpAdapter: "2.20.1",
    });
  });

  it("asserts the in-memory SDK capabilities without starting a model call", () => {
    expect(assertPiSdkContract()).toEqual({
      createAgentSession: true,
      inMemorySessionManager: true,
      toolCallEvents: true,
      abort: true,
      dispose: true,
    });
  });

  it("keeps the public session boundary free of secret-bearing fields", () => {
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
        rootPolicy: "root-policy",
        skillCatalog: [],
        adapterCatalog: [],
      },
    };
    const secrets: SecretBundle = {
      modelBaseUrl: "http://127.0.0.1:1",
      modelApiKey: "fake-key",
      datatapToken: "fake-datatap",
      datatapUrls: { insightCube: "http://127.0.0.1:2" },
    };
    const event: PiSdkEvent = { type: "session_start" };
    const session: PiRunSession = {
      prompt: async () => undefined,
      subscribe: () => () => undefined,
      abort: async () => undefined,
      dispose: async () => undefined,
      systemPrompt: () => "",
      activeToolNames: () => [],
      cwd: () => "/tmp/run",
    };

    expect(JSON.stringify({ work, event, session })).not.toContain("fake-key");
    expect(JSON.stringify({ work, event, session })).not.toContain("fake-datatap");
  });
});
