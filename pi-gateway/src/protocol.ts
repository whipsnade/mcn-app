import {
  AgentSession,
  AuthStorage,
  createAgentSession,
  ModelRegistry,
  SessionManager,
  SettingsManager,
} from "@earendil-works/pi-coding-agent";
import type { McpAccountingExtension } from "./mcp-accounting-extension.js";

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

export interface RuntimeSecretEnvelope {
  alg: "AES-256-GCM";
  nonce: string;
  ciphertext: string;
}

export interface PiGatewayAdapterCatalogEntry {
  catalog_entry_id: string;
  adapter_visible_name: string;
  service: string;
  remote_name: string;
  input_schema_digest: string;
}

export interface PiGatewaySourceEvent {
  source_event_id: string;
  sequence: number;
  event_type: string;
  payload: Record<string, unknown>;
}

export interface PiGatewayClaimResponse {
  run_id: string;
  attempt_id: string;
  lease_token: string;
  runtime_snapshot: Record<string, unknown>;
  transcript: Array<Record<string, unknown>>;
  secret_envelope: RuntimeSecretEnvelope;
  adapter_catalog: PiGatewayAdapterCatalogEntry[];
  internal_tools: Array<Record<string, unknown>>;
}

const PI_ADAPTER_SERVICE_ALIASES: Readonly<Record<string, string>> = Object.freeze({
  "insight-cube-mcp": "insight-cube",
  "social-grow-mcp": "social-grow",
  "social-grow-content-mcp": "social-grow-content",
  "aktools-mcp": "aktools",
  "bilibili-mcp": "aktools",
});

/**
 * Convert the authenticated FastAPI wire catalog into the camelCase catalog
 * consumed by the Pi resource loader.  The conversion is deliberately kept
 * at this boundary: persisted/runtime snapshots remain the server-owned
 * snake_case contract and are never guessed by the SDK session.
 */
export function normalizePiGatewayAdapterCatalog(
  entries: readonly PiGatewayAdapterCatalogEntry[],
): AdapterCatalogEntry[] {
  const seen = new Set<string>();
  return entries.map((entry) => {
    const service = PI_ADAPTER_SERVICE_ALIASES[entry.service] ?? entry.service;
    const key = `${service}\u0000${entry.adapter_visible_name}`;
    if (!service || !entry.adapter_visible_name || seen.has(key)) {
      throw new Error("pi_gateway_adapter_catalog_invalid");
    }
    seen.add(key);
    if (!/^sha256:[0-9a-fA-F]{64}$/.test(entry.input_schema_digest)) {
      throw new Error("pi_gateway_adapter_catalog_invalid");
    }
    return {
      service,
      adapterName: entry.adapter_visible_name,
      remoteName: entry.remote_name,
      schemaDigest: entry.input_schema_digest,
    };
  });
}

/**
 * Keep the authenticated wire response immutable while attaching the
 * normalized catalog used by the worker/session boundary.  The original
 * snake_case fields remain available for audit/HTTP diagnostics; production
 * code must read ``runtime_snapshot.adapterCatalog`` after this conversion.
 */
export function normalizePiGatewayClaimResponse(
  response: PiGatewayClaimResponse,
): PiGatewayClaimResponse {
  const adapterCatalog = normalizePiGatewayAdapterCatalog(response.adapter_catalog);
  return {
    ...response,
    runtime_snapshot: {
      ...response.runtime_snapshot,
      adapterCatalog,
    },
  };
}

const PI_GATEWAY_SOURCE_EVENT_TYPES = new Set([
  "agent.turn.start", "agent.turn.end", "agent/turn/start", "agent/turn/end",
  "message.start", "message.delta", "message.end", "message.completed",
  "message/start", "message/delta", "message/end", "tool.start", "tool.end",
  "tool_call.start", "tool_call.end", "tool/start", "tool/end", "thinking.start",
  "thinking.delta", "thinking.end", "thinking/start", "thinking/delta", "thinking/end",
  "text.delta", "text/delta", "usage",
]);

const PAYLOAD_SENSITIVE_KEYS = new Set([
  "authorization", "api_key", "apikey", "password", "secret", "token", "environment",
  "tenant_id", "user_id", "session_id", "run_id", "attempt_id", "gateway_id", "lease_token",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function exactKeys(value: Record<string, unknown>, expected: readonly string[]): boolean {
  const keys = Object.keys(value).sort();
  return keys.length === expected.length && keys.every((key, index) => key === [...expected].sort()[index]);
}

function hasSensitiveKey(value: unknown): boolean {
  if (Array.isArray(value)) return value.some(hasSensitiveKey);
  if (!isRecord(value)) return false;
  return Object.entries(value).some(([key, item]) => PAYLOAD_SENSITIVE_KEYS.has(key.toLowerCase()) || hasSensitiveKey(item));
}

function invalidProtocol(): never {
  throw new Error("pi_gateway_source_event_invalid");
}

/** Runtime validation mirror for the strict FastAPI source-event DTO. */
export function parsePiGatewaySourceEvent(value: unknown): PiGatewaySourceEvent {
  if (!isRecord(value) || !exactKeys(value, ["source_event_id", "sequence", "event_type", "payload"])) invalidProtocol();
  const sourceEventId = value.source_event_id;
  const sequence = value.sequence;
  const sequenceNumber = sequence as number;
  const eventType = value.event_type;
  const payload = value.payload;
  if (
    typeof sourceEventId !== "string" ||
    !/^[A-Za-z0-9._:-]{1,160}$/.test(sourceEventId) ||
    !Number.isInteger(sequenceNumber) || sequenceNumber < 1 || sequenceNumber > 10_000_000 ||
    typeof eventType !== "string" || !PI_GATEWAY_SOURCE_EVENT_TYPES.has(eventType) ||
    !isRecord(payload) ||
    new TextEncoder().encode(JSON.stringify(payload)).byteLength > 64 * 1024 ||
    hasSensitiveKey(payload)
  ) invalidProtocol();
  const suffix = sourceEventId.slice(sourceEventId.lastIndexOf(":") + 1);
  if (Number(suffix) !== sequenceNumber) invalidProtocol();
  if (["message.delta", "text.delta", "thinking.delta"].includes(eventType)) {
    for (const key of ["delta", "text"]) {
      if (typeof payload[key] === "string" && (payload[key] as string).length > 16_384) invalidProtocol();
    }
  }
  return { source_event_id: sourceEventId, sequence: sequenceNumber, event_type: eventType, payload };
}

/** Runtime validation mirror for the strict FastAPI claim response DTO. */
export function parsePiGatewayClaimResponse(value: unknown): PiGatewayClaimResponse {
  if (!isRecord(value) || !exactKeys(value, ["run_id", "attempt_id", "lease_token", "runtime_snapshot", "transcript", "secret_envelope", "adapter_catalog", "internal_tools"])) {
    throw new Error("pi_gateway_claim_response_invalid");
  }
  const envelope = value.secret_envelope;
  if (
    typeof value.run_id !== "string" || value.run_id.length < 1 || value.run_id.length > 64 ||
    typeof value.attempt_id !== "string" || value.attempt_id.length < 1 || value.attempt_id.length > 64 ||
    typeof value.lease_token !== "string" || value.lease_token.length < 32 || value.lease_token.length > 512 ||
    !isRecord(value.runtime_snapshot) || !Array.isArray(value.transcript) || value.transcript.length > 100 ||
    !isRecord(envelope) || !exactKeys(envelope, ["alg", "nonce", "ciphertext"]) ||
    envelope.alg !== "AES-256-GCM" || typeof envelope.nonce !== "string" || envelope.nonce.length < 16 || envelope.nonce.length > 64 ||
    typeof envelope.ciphertext !== "string" || envelope.ciphertext.length < 16 || envelope.ciphertext.length > 200_000 ||
    !Array.isArray(value.adapter_catalog) || value.adapter_catalog.length > 32 ||
    !Array.isArray(value.internal_tools) || value.internal_tools.length > 64
  ) throw new Error("pi_gateway_claim_response_invalid");
  if (
    new TextEncoder().encode(JSON.stringify(value.runtime_snapshot)).byteLength > 256 * 1024 ||
    value.transcript.some((item) => {
      if (!isRecord(item) || !exactKeys(item, ["role", "content"])) return true;
      return (
        (item.role !== "user" && item.role !== "assistant") ||
        typeof item.content !== "string" || item.content.length > 32_000 ||
        hasSensitiveKey(item)
      );
    }) ||
    value.adapter_catalog.some((item) => {
      if (!isRecord(item) || !exactKeys(item, ["catalog_entry_id", "adapter_visible_name", "service", "remote_name", "input_schema_digest"])) return true;
      return (
        typeof item.catalog_entry_id !== "string" || item.catalog_entry_id.length < 1 || item.catalog_entry_id.length > 64 ||
        typeof item.adapter_visible_name !== "string" || item.adapter_visible_name.length < 1 || item.adapter_visible_name.length > 128 ||
        typeof item.service !== "string" || item.service.length < 1 || item.service.length > 64 ||
        typeof item.remote_name !== "string" || item.remote_name.length < 1 || item.remote_name.length > 128 ||
        typeof item.input_schema_digest !== "string" || !/^sha256:[0-9a-fA-F]{64}$/.test(item.input_schema_digest)
      );
    }) ||
    value.internal_tools.some((item) => {
      if (!isRecord(item) || !exactKeys(item, ["name"])) return true;
      return typeof item.name !== "string" || item.name.length < 1 || item.name.length > 128;
    }) ||
    hasSensitiveKey(value.runtime_snapshot)
  ) throw new Error("pi_gateway_claim_response_invalid");
  return value as unknown as PiGatewayClaimResponse;
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
  /** Adapter-facing hook; the SDK session itself never receives wallet data. */
  mcpAccounting?: McpAccountingExtension;
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
