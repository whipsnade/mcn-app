import { mkdtemp, mkdir, rm, writeFile, chmod } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  AuthStorage,
  createAgentSession,
  ModelRegistry,
  SessionManager,
  SettingsManager,
} from "@earendil-works/pi-coding-agent";
import {
  fauxAssistantMessage,
  createAssistantMessageEventStream,
  type Context,
  type Model,
  type SimpleStreamOptions,
} from "@earendil-works/pi-ai";

import { adapterServerName, createMcpConfig, createProductionResourceLoader } from "./resource-loader.js";
import {
  createMcpAccountingExtensionFactory,
  McpAccountingExtension,
  type McpAccountingControlPlane,
} from "./mcp-accounting-extension.js";
import {
  assertCompletePiSdkContract,
  PI_ALLOWED_TOOL_NAMES,
  type ClaimedRun,
  type PiRunSession,
  type PiSdkEvent,
  type SecretBundle,
} from "./protocol.js";

export interface PiSessionOptions {
  fakeProvider?: boolean;
  /** Optional control-plane bridge used by the MCP adapter hook. */
  mcpAccounting?: McpAccountingControlPlane;
}

export async function createProductionPiSession(
  work: ClaimedRun,
  secrets: SecretBundle,
  options: PiSessionOptions = {},
): Promise<PiRunSession> {
  assertCompletePiSdkContract();
  const runDir = await mkdtemp(join(tmpdir(), "kol-pi-run-"));
  await chmod(runDir, 0o700);
  const agentDir = join(runDir, "agent");
  await mkdir(agentDir, { mode: 0o700 });
  await writeFile(join(runDir, ".mcp.json"), JSON.stringify(createMcpConfig(work.runtimeSnapshot.adapterCatalog), null, 2), {
    encoding: "utf8",
    mode: 0o600,
  });

  let disposed = false;
  try {
    const mcpAccounting = options.mcpAccounting
      ? new McpAccountingExtension(options.mcpAccounting)
      : undefined;
    const authStorage = AuthStorage.inMemory();
    authStorage.setRuntimeApiKey(work.runtimeSnapshot.model.provider, secrets.modelApiKey);
    const modelRegistry = ModelRegistry.inMemory(authStorage);
    const model = registerModel(modelRegistry, work, secrets, options.fakeProvider === true);
    const loader = createProductionResourceLoader({
      cwd: runDir,
      agentDir,
      rootPolicy: work.runtimeSnapshot.rootPolicy,
      skillCatalog: work.runtimeSnapshot.skillCatalog,
      adapterCatalog: work.runtimeSnapshot.adapterCatalog,
      extensionFactories: mcpAccounting
        ? [createMcpAccountingExtensionFactory(mcpAccounting, work.runtimeSnapshot.adapterCatalog.map((entry, index, catalog) => ({
          toolName: entry.adapterName,
          server: adapterServerName(entry, index, catalog),
          remoteName: entry.remoteName,
        })))]
        : undefined,
    });
    await loader.reload();
    const settingsManager = SettingsManager.inMemory({
      defaultProvider: model.provider,
      defaultModel: model.id,
      defaultThinkingLevel: work.runtimeSnapshot.model.thinkingLevel ?? "medium",
    });
    const sessionManager = SessionManager.inMemory(runDir);
    const { session } = await createAgentSession({
      cwd: runDir,
      agentDir,
      authStorage,
      modelRegistry,
      model,
      thinkingLevel: work.runtimeSnapshot.model.thinkingLevel ?? "medium",
      noTools: "builtin",
      tools: [...PI_ALLOWED_TOOL_NAMES],
      resourceLoader: loader,
      sessionManager,
      settingsManager,
    });
    const listeners = new Set<(event: PiSdkEvent) => void>();
    const unsubscribeSdk = session.subscribe((event) => {
      for (const listener of listeners) {
        listener({ type: "sdk_event", eventType: event.type, event });
      }
    });
    for (const listener of listeners) listener({ type: "session_start" });
    return {
      prompt: async (content) => {
        for (const listener of listeners) listener({ type: "user_prompt", content });
        await session.prompt(content);
      },
      subscribe: (listener) => {
        listeners.add(listener);
        listener({ type: "session_start" });
        return () => listeners.delete(listener);
      },
      abort: () => session.abort(),
      dispose: async () => {
        if (disposed) return;
        disposed = true;
        try {
          try {
            unsubscribeSdk();
          } finally {
            session.dispose();
          }
        } finally {
          try {
            for (const listener of listeners) listener({ type: "session_end" });
          } finally {
            listeners.clear();
            await rm(runDir, { recursive: true, force: true });
          }
        }
      },
      systemPrompt: () => session.systemPrompt,
      activeToolNames: () => session.getActiveToolNames(),
      cwd: () => runDir,
      mcpAccounting,
    };
  } catch (error) {
    await rm(runDir, { recursive: true, force: true });
    throw error;
  }
}

function registerModel(
  registry: ModelRegistry,
  work: ClaimedRun,
  secrets: SecretBundle,
  fakeProvider: boolean,
): Model<any> {
  const provider = work.runtimeSnapshot.model.provider;
  const id = work.runtimeSnapshot.model.id;
  const api = fakeProvider ? "faux" : work.runtimeSnapshot.model.api;
  registry.registerProvider(provider, {
    api,
    baseUrl: secrets.modelBaseUrl,
    apiKey: secrets.modelApiKey,
    authHeader: true,
    // The locked coding-agent package carries its own pi-ai declaration; the
    // runtime stream protocol is identical, so bridge the duplicate private
    // EventStream type at this one SDK registration boundary.
    streamSimple: fakeProvider ? fakeStream as any : undefined,
    models: [{
      id,
      name: id,
      api,
      reasoning: true,
      input: ["text"],
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      contextWindow: 32_000,
      maxTokens: 2_000,
    }],
  });
  const model = registry.find(provider, id);
  if (!model) throw new Error("pi_model_registration_failed");
  return model;
}

function fakeStream(
  _model: Model<any>,
  _context: Context,
  _options?: SimpleStreamOptions,
): ReturnType<typeof createAssistantMessageEventStream> {
  const stream = createAssistantMessageEventStream();
  const message = fauxAssistantMessage("fake provider response");
  queueMicrotask(() => {
    stream.push({ type: "start", partial: message });
    stream.push({ type: "text_start", contentIndex: 0, partial: message });
    stream.push({ type: "text_delta", contentIndex: 0, delta: "fake provider response", partial: message });
    stream.push({ type: "text_end", contentIndex: 0, content: "fake provider response", partial: message });
    stream.push({ type: "done", reason: "stop", message });
  });
  return stream;
}
