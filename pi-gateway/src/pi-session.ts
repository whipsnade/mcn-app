import { mkdtemp, mkdir, rm, writeFile, chmod } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  AuthStorage,
  createAgentSession,
  ModelRegistry,
  SessionManager,
  SettingsManager,
  type ExtensionFactory,
} from "@earendil-works/pi-coding-agent";
import {
  fauxAssistantMessage,
  fauxToolCall,
  createAssistantMessageEventStream,
  type Context,
  type Model,
  type SimpleStreamOptions,
} from "@earendil-works/pi-ai";

import { createMcpConfig, createProductionResourceLoader } from "./resource-loader.js";
import {
  createMcpAccountingExtensionFactory,
  McpAccountingExtension,
  type McpAccountingControlPlane,
} from "./mcp-accounting-extension.js";
import { buildInternalToolDefinitions, PiInternalToolsClient } from "./internal-tools.js";
import type { ControlPlaneTransport } from "./control-plane-client.js";
import {
  assertCompletePiSdkContract,
  PI_ALLOWED_TOOL_NAMES,
  type AdapterCatalogEntry,
  type ClaimedRun,
  type FakeScriptStep,
  type PiRunSession,
  type PiSdkEvent,
  type SecretBundle,
} from "./protocol.js";

export interface PiSessionOptions {
  fakeProvider?: boolean;
  /** Offline/test-only scripted responses; requires fakeProvider. */
  fakeScript?: readonly FakeScriptStep[];
  /** Optional control-plane bridge used by the MCP adapter hook. */
  mcpAccounting?: McpAccountingControlPlane;
  /** Optional control-plane bridge for the reviewed internal tools. */
  internalTools?: ControlPlaneTransport;
}

const FAKE_TOOL_NAME = /^[A-Za-z0-9._:-]{1,128}$/;

function validateFakeScript(script: readonly FakeScriptStep[] | undefined): void {
  if (script === undefined) return;
  if (script.length === 0 || script.length > 16) throw new Error("pi_fake_script_invalid");
  for (const step of script) {
    if (step.kind === "text") {
      if (typeof step.text !== "string" || step.text.length === 0 || step.text.length > 16_384) {
        throw new Error("pi_fake_script_invalid");
      }
      continue;
    }
    if (step.kind === "tool_call") {
      if (
        !FAKE_TOOL_NAME.test(step.tool) ||
        (step.tool !== "mcp" && !(PI_ALLOWED_TOOL_NAMES as readonly string[]).includes(step.tool)) ||
        !step.args ||
        typeof step.args !== "object" ||
        Array.isArray(step.args) ||
        Object.keys(step.args).length > 64
      ) {
        throw new Error("pi_fake_script_invalid");
      }
      continue;
    }
    throw new Error("pi_fake_script_invalid");
  }
}

function normalizeServiceKey(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]/g, "");
}

/**
 * Resolve the reviewed pi-mcp-adapter entrypoint for the resource loader.
 * The SDK loads it with jiti at runtime, so the TypeScript-only package never
 * enters this project's own typecheck or build graph.
 */
export function resolveMcpAdapterExtensionPath(): string {
  return fileURLToPath(import.meta.resolve("pi-mcp-adapter"));
}

/**
 * Point the adapter's default config discovery (argv ``--mcp-config``) at the
 * current Run's rendered `.mcp.json`.  Each isolated worker process serves
 * exactly one Run; any previous entry is replaced.
 */
export function setAdapterMcpConfigPath(configPath: string): void {
  const index = process.argv.indexOf("--mcp-config");
  if (index >= 0) process.argv.splice(index, 2);
  process.argv.push("--mcp-config", configPath);
}

/**
 * Assert every catalogued service has a decrypted endpoint in the Run bundle.
 * Endpoint and token values stay in the child process environment; the
 * on-disk `.mcp.json` only carries references to their variable names.
 */
export function assertDatatapUrlsForCatalog(
  catalog: readonly AdapterCatalogEntry[],
  secrets: SecretBundle,
): void {
  const available = new Set(Object.keys(secrets.datatapUrls).map(normalizeServiceKey));
  for (const entry of catalog) {
    if (!available.has(normalizeServiceKey(entry.service))) {
      throw new Error("pi_datatap_url_missing");
    }
  }
}

export async function createProductionPiSession(
  work: ClaimedRun,
  secrets: SecretBundle,
  options: PiSessionOptions = {},
): Promise<PiRunSession> {
  assertCompletePiSdkContract();
  validateFakeScript(options.fakeScript);
  if (options.fakeScript !== undefined && options.fakeProvider !== true) {
    throw new Error("pi_fake_script_requires_fake_provider");
  }
  const runDir = await mkdtemp(join(tmpdir(), "kol-pi-run-"));
  await chmod(runDir, 0o700);
  const agentDir = join(runDir, "agent");
  await mkdir(agentDir, { mode: 0o700 });
  const mcpConfigPath = join(runDir, ".mcp.json");
  await writeFile(mcpConfigPath, JSON.stringify(createMcpConfig(work.runtimeSnapshot.adapterCatalog), null, 2), {
    encoding: "utf8",
    mode: 0o600,
  });
  // The adapter's default export discovers this Run's config via argv
  // ``--mcp-config`` (jiti-loaded extension runs in this same child process).
  setAdapterMcpConfigPath(mcpConfigPath);

  let disposed = false;
  try {
    assertDatatapUrlsForCatalog(work.runtimeSnapshot.adapterCatalog, secrets);
    const mcpAccounting = options.mcpAccounting
      ? new McpAccountingExtension(options.mcpAccounting)
      : undefined;
    const extensionFactories: ExtensionFactory[] = mcpAccounting
      ? [createMcpAccountingExtensionFactory(mcpAccounting, work.runtimeSnapshot.adapterCatalog.map((entry) => ({
        toolName: entry.adapterName,
        server: entry.service,
        remoteName: entry.remoteName,
      })))]
      : [];
    const internalTools = options.internalTools
      ? buildInternalToolDefinitions(
          new PiInternalToolsClient(options.internalTools),
          work.internalTools,
        )
      : [];
    const authStorage = AuthStorage.inMemory();
    authStorage.setRuntimeApiKey(work.runtimeSnapshot.model.provider, secrets.modelApiKey);
    const modelRegistry = ModelRegistry.inMemory(authStorage);
    const model = registerModel(modelRegistry, work, secrets, options.fakeProvider === true, options.fakeScript);
    const loader = createProductionResourceLoader({
      cwd: runDir,
      agentDir,
      rootPolicy: work.runtimeSnapshot.rootPolicy,
      skillCatalog: work.runtimeSnapshot.skillCatalog,
      adapterCatalog: work.runtimeSnapshot.adapterCatalog,
      adapterExtension: resolveMcpAdapterExtensionPath(),
      extensionFactories,
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
      customTools: internalTools,
      resourceLoader: loader,
      sessionManager,
      settingsManager,
    });
    // The SDK createAgentSession path never fires the extension session_start
    // event (the CLI does); the reviewed adapter initializes its MCP runtime
    // state on that event.  bindExtensions({}) is the public startup emit.
    await session.bindExtensions({});
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
  fakeScript?: readonly FakeScriptStep[],
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
    streamSimple: fakeProvider ? fakeStreamFactory(fakeScript) as any : undefined,
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

function selectFakeStep(script: readonly FakeScriptStep[] | undefined, context: Context): FakeScriptStep {
  if (!script || script.length === 0) return { kind: "text", text: "fake provider response" };
  const assistantRounds = context.messages.filter((message) => message.role === "assistant").length;
  return script[Math.min(assistantRounds, script.length - 1)];
}

function fakeStreamFactory(script: readonly FakeScriptStep[] | undefined) {
  return function fakeStream(
    _model: Model<any>,
    context: Context,
    _options?: SimpleStreamOptions,
  ): ReturnType<typeof createAssistantMessageEventStream> {
    const step = selectFakeStep(script, context);
    const stream = createAssistantMessageEventStream();
    queueMicrotask(() => {
      if (step.kind === "tool_call") {
        const toolCall = fauxToolCall(step.tool, step.args, { id: `fake-tool-${step.tool}` });
        const message = fauxAssistantMessage([toolCall], { stopReason: "toolUse" });
        stream.push({ type: "start", partial: message });
        stream.push({ type: "toolcall_start", contentIndex: 0, partial: message });
        stream.push({ type: "toolcall_end", contentIndex: 0, toolCall, partial: message });
        stream.push({ type: "done", reason: "toolUse", message });
        return;
      }
      const message = fauxAssistantMessage(step.text);
      stream.push({ type: "start", partial: message });
      stream.push({ type: "text_start", contentIndex: 0, partial: message });
      stream.push({ type: "text_delta", contentIndex: 0, delta: step.text, partial: message });
      stream.push({ type: "text_end", contentIndex: 0, content: step.text, partial: message });
      stream.push({ type: "done", reason: "stop", message });
    });
    return stream;
  };
}
