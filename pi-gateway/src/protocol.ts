import {
  AgentSession,
  AuthStorage,
  createAgentSession,
  ModelRegistry,
  SessionManager,
  SettingsManager,
} from "@earendil-works/pi-coding-agent";

export const PI_DEPENDENCY_VERSIONS = Object.freeze({
  codingAgent: "0.79.10",
  piAi: "0.74.2",
  piTui: "0.74.2",
  mcpAdapter: "2.20.1",
});

export const PI_ALLOWED_TOOL_NAMES = Object.freeze([
  "mcp",
  "get_session_context",
  "load_marketing_skill",
  "search_evidence",
  "read_tool_result",
  "read_artifact",
  "build_brand_report_draft",
  "build_campaign_report_draft",
  "build_kol_selection_draft",
  "build_kol_analysis_draft",
  "build_kol_detail_draft",
  "build_insight_draft",
  "publish_artifacts",
  "request_clarification",
] as const);

export interface SkillCatalogEntry {
  name: string;
  description: string;
  version: string;
  artifactContract: string;
}

export interface AdapterCatalogEntry {
  service: string;
  adapterName: string;
  remoteName: string;
  schemaDigest: string;
}

export interface RuntimeModelSnapshot {
  provider: string;
  id: string;
  api: string;
  thinkingLevel?: "minimal" | "low" | "medium" | "high" | "xhigh";
}

export interface RuntimeSnapshot {
  configVersionId: string;
  model: RuntimeModelSnapshot;
  rootPolicy: string;
  skillCatalog: readonly SkillCatalogEntry[];
  adapterCatalog: readonly AdapterCatalogEntry[];
}

export interface ClaimedRun {
  runId: string;
  tenantId: string;
  userId: string;
  sessionId: string;
  attemptId: string;
  runtimeBackend: "pi";
  runtimeSnapshot: RuntimeSnapshot;
  userPrompt?: string;
}

export interface SecretBundle {
  modelBaseUrl: string;
  modelApiKey: string;
  datatapToken: string;
  datatapUrls: Readonly<Record<string, string>>;
}

export type PiSdkEvent =
  | { type: "session_start" }
  | { type: "user_prompt"; content: string }
  | { type: "sdk_event"; eventType: string }
  | { type: "session_end" }
  | { type: "error"; code: string };

export interface PiRunSession {
  prompt(content: string): Promise<void>;
  subscribe(listener: (event: PiSdkEvent) => void): () => void;
  abort(): Promise<void>;
  dispose(): Promise<void>;
  systemPrompt(): string;
  activeToolNames(): readonly string[];
  cwd(): string;
}

export interface PiSessionFactory {
  create(work: ClaimedRun, secrets: SecretBundle): Promise<PiRunSession>;
}

export interface PiSdkContractStatus {
  createAgentSession: boolean;
  inMemorySessionManager: boolean;
  toolCallEvents: boolean;
  abort: boolean;
  dispose: boolean;
}

/**
 * Runtime-only assertion for the exact SDK surface pinned by this package.
 * It does not create a model session or perform any provider call.
 */
export function assertPiSdkContract(): PiSdkContractStatus {
  const prototype = AgentSession.prototype as unknown as Record<string, unknown>;
  return {
    createAgentSession: typeof createAgentSession === "function",
    inMemorySessionManager: typeof SessionManager.inMemory === "function",
    toolCallEvents: typeof prototype.subscribe === "function",
    abort: typeof prototype.abort === "function",
    dispose: typeof prototype.dispose === "function",
  };
}

export function assertCompletePiSdkContract(): void {
  const status = assertPiSdkContract();
  if (Object.values(status).some((value) => !value)) {
    throw new Error("pi_sdk_contract_unsupported");
  }
  if (
    typeof AuthStorage.inMemory !== "function" ||
    typeof ModelRegistry.inMemory !== "function" ||
    typeof SettingsManager.inMemory !== "function"
  ) {
    throw new Error("pi_sdk_memory_storage_unsupported");
  }
}
