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

/**
 * The single canonical mounted base path of the FastAPI internal protocol.
 * HMAC signatures always cover the full mounted path below, never a relative
 * suffix; the backend verifies ``request.url.path`` byte-for-byte.
 */
export const CONTROL_PLANE_BASE_PATH = "/api/v1/internal/pi-gateway/v1";

export const PI_ALLOWED_TOOL_NAMES = Object.freeze([
  "mcp",
  "get_session_context",
  "load_marketing_skill",
  "read_artifact",
  "build_artifact_draft",
  "publish_artifacts",
  "request_clarification",
] as const);

export interface SkillCatalogEntry {
  name: string;
  description: string;
  version: string;
  artifactContract: string | null;
}

export interface SkillSnapshotEntry {
  name: string;
  revision: number;
  contentDigest: string;
  description: string;
  requiredTools: readonly string[];
  artifactContract: string | null;
  content: string;
}

export interface SkillManifestSnapshot {
  entries: readonly SkillSnapshotEntry[];
  manifestDigest: string;
  sourceScope: "database_activation" | "legacy_pack";
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
  skillManifest?: SkillManifestSnapshot;
  adapterCatalog: readonly AdapterCatalogEntry[];
  /** Server-resolved profile capability captured at Run creation. */
  profileName?: string;
  /** Candidate artifact contracts frozen by the server; never a required target. */
  allowedArtifactContracts: readonly string[];
  capabilityPackVersion?: string;
  capabilityPackManifestDigest?: string;
  /**
   * Server-owned per-Run model decision budget (runtime config
   * ``limits.max_decisions``).  Enforced synchronously at the provider
   * dispatch boundary; there is deliberately no gateway-side default.
   */
  maxDecisions: number;
}

/**
 * Offline/test-only scripted fake provider step.  The production composition
 * root never populates ``ClaimedRun.fakeScript``; it exists so isolated-child
 * tests and the offline UAT can drive deterministic tool-call rounds through
 * the real Pi SDK without any external model call.
 */
export type FakeScriptStep =
  | { kind: "text"; text: string }
  | { kind: "tool_call"; tool: string; args: Record<string, unknown> };

export interface ClaimedRun {
  runId: string;
  attemptId: string;
  runtimeBackend: "pi";
  runtimeSnapshot: RuntimeSnapshot;
  userPrompt?: string;
  /** Claim-allowed internal tool names; the child registers only these. */
  internalTools?: readonly string[];
  /** Offline/test-only fake provider script; never set by production claim. */
  fakeScript?: readonly FakeScriptStep[];
  /**
   * Tenant identity is owned by the FastAPI control plane and bound to the
   * Run lease.  Production workers never receive it; these optional fields
   * exist only for in-process test doubles.
   */
  tenantId?: string;
  userId?: string;
  sessionId?: string;
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

export interface PiGatewaySourceEventBatch {
  events: PiGatewaySourceEvent[];
}

export interface PiGatewaySourceEventReceipt {
  source_event_id: string;
  sequence: number;
  duplicate: boolean;
  event_id?: string;
  usage_record_id?: string;
}

export interface PiGatewaySourceEventBatchReceipt {
  receipts: PiGatewaySourceEventReceipt[];
  last_acked_source_sequence: number;
}

export interface PiGatewayClaimResponse {
  run_id: string;
  attempt_id: string;
  lease_token: string;
  /** 明确的 lease deadline（epoch 秒）；缺省视为协议违规，禁止执行。 */
  lease_expires_at: number;
  runtime_snapshot: Record<string, unknown>;
  transcript: Array<Record<string, unknown>>;
  secret_envelope: RuntimeSecretEnvelope;
  adapter_catalog: PiGatewayAdapterCatalogEntry[];
  internal_tools: Array<Record<string, unknown>>;
}

// Control-plane transport bounds only; directTools=false still exposes one
// model-visible MCP proxy, not one top-level tool per catalog entry.
export const PI_GATEWAY_ADAPTER_CATALOG_MAX_ENTRIES = 128;
export const PI_GATEWAY_ADAPTER_CATALOG_MAX_BYTES = 128 * 1024;
export const PI_GATEWAY_EVENT_BATCH_MAX_EVENTS = 32;
export const PI_GATEWAY_EVENT_BATCH_MAX_BYTES = 128 * 1024;

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
  assertAdapterCatalogBounds(entries);
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

function canonicalizeJson(value: unknown): unknown {
  if (Array.isArray(value)) return value.map((item) => canonicalizeJson(item));
  if (isRecord(value)) {
    return Object.fromEntries(
      Object.keys(value).sort().map((key) => [key, canonicalizeJson(value[key])]),
    );
  }
  return value;
}

function adapterCatalogCanonicalJsonBytes(value: readonly unknown[]): number {
  const serialized = JSON.stringify(canonicalizeJson(value));
  return new TextEncoder().encode(serialized ?? "null").byteLength;
}

function assertAdapterCatalogBounds(entries: readonly unknown[]): void {
  if (
    entries.length > PI_GATEWAY_ADAPTER_CATALOG_MAX_ENTRIES ||
    adapterCatalogCanonicalJsonBytes(entries) > PI_GATEWAY_ADAPTER_CATALOG_MAX_BYTES
  ) {
    throw new Error("pi_gateway_adapter_catalog_invalid");
  }
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
  "turn.start", "turn/start",
  "message.start", "message.delta", "message.end", "message.completed",
  "message/start", "message/delta", "message/end", "tool.start", "tool.end",
  "tool_call.start", "tool_call.end", "tool_call/start", "tool_call/end",
  "tool/start", "tool/end", "thinking.start",
  "thinking.delta", "thinking.end", "thinking/start", "thinking/delta", "thinking/end",
  "text.delta", "text/delta", "usage",
]);

const PAYLOAD_SENSITIVE_KEYS = new Set([
  "authorization", "api_key", "apikey", "password", "secret", "token", "environment",
  "tenant_id", "user_id", "session_id", "run_id", "attempt_id", "gateway_id", "lease_token",
]);
// ``environment`` is a server-owned field of the authenticated Runtime Snapshot.
// It remains forbidden in model/source-event payloads, but must be accepted
// here so the Gateway can run an explicitly selected non-production profile.
const RUNTIME_SNAPSHOT_SENSITIVE_KEYS = new Set([
  "authorization", "api_key", "apikey", "password", "secret", "token",
  "tenant_id", "user_id", "session_id", "run_id", "attempt_id", "gateway_id", "lease_token",
]);
const USAGE_PAYLOAD_KEYS = new Set([
  "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
  "upstream_request_id", "provider", "model", "usage_status",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function exactKeys(value: Record<string, unknown>, expected: readonly string[]): boolean {
  const keys = Object.keys(value).sort();
  return keys.length === expected.length && keys.every((key, index) => key === [...expected].sort()[index]);
}

function hasSensitiveKey(value: unknown, sensitiveKeys: Set<string> = PAYLOAD_SENSITIVE_KEYS): boolean {
  if (Array.isArray(value)) return value.some((item) => hasSensitiveKey(item, sensitiveKeys));
  if (!isRecord(value)) return false;
  return Object.entries(value).some(
    ([key, item]) => sensitiveKeys.has(key.toLowerCase()) || hasSensitiveKey(item, sensitiveKeys),
  );
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
  if (eventType === "usage") {
    if (Object.keys(payload).some((key) => !USAGE_PAYLOAD_KEYS.has(key))) invalidProtocol();
    for (const key of ["input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"]) {
      if (key in payload && (
        typeof payload[key] !== "number" || !Number.isInteger(payload[key]) ||
        (payload[key] as number) < 0 || (payload[key] as number) > 1e12
      )) invalidProtocol();
    }
    if (
      "upstream_request_id" in payload &&
      (typeof payload.upstream_request_id !== "string" || payload.upstream_request_id.length < 1 || payload.upstream_request_id.length > 128)
    ) invalidProtocol();
    if (
      "usage_status" in payload && payload.usage_status !== "available" && payload.usage_status !== "unavailable"
    ) invalidProtocol();
  }
  return { source_event_id: sourceEventId, sequence: sequenceNumber, event_type: eventType, payload };
}

export function parsePiGatewaySourceEventBatch(value: unknown): PiGatewaySourceEventBatch {
  if (!isRecord(value) || !exactKeys(value, ["events"]) || !Array.isArray(value.events)) {
    invalidProtocol();
  }
  if (value.events.length < 1 || value.events.length > PI_GATEWAY_EVENT_BATCH_MAX_EVENTS) {
    invalidProtocol();
  }
  const events = value.events.map((event) => parsePiGatewaySourceEvent(event));
  const attemptIds = new Set(
    events.map((event) => event.source_event_id.slice(0, event.source_event_id.lastIndexOf(":"))),
  );
  if (attemptIds.size !== 1) invalidProtocol();
  for (let index = 1; index < events.length; index += 1) {
    if (events[index].sequence !== events[index - 1].sequence + 1) invalidProtocol();
  }
  const bytes = piGatewaySourceEventBatchBytes(events);
  if (bytes > PI_GATEWAY_EVENT_BATCH_MAX_BYTES) invalidProtocol();
  return { events };
}

export function piGatewaySourceEventBatchBytes(events: readonly PiGatewaySourceEvent[]): number {
  return new TextEncoder().encode(JSON.stringify(canonicalizeJson({ events }))).byteLength;
}

export function parsePiGatewaySourceEventBatchReceipt(value: unknown): PiGatewaySourceEventBatchReceipt {
  if (!isRecord(value) || !exactKeys(value, ["receipts", "last_acked_source_sequence"]) || !Array.isArray(value.receipts)) {
    throw new Error("pi_gateway_event_batch_receipt_invalid");
  }
  const lastSequence = value.last_acked_source_sequence;
  if (!Number.isInteger(lastSequence) || (lastSequence as number) < 1 || (lastSequence as number) > 10_000_000) {
    throw new Error("pi_gateway_event_batch_receipt_invalid");
  }
  const receipts = value.receipts.map((raw) => {
    if (!isRecord(raw)) throw new Error("pi_gateway_event_batch_receipt_invalid");
    const allowedKeys = new Set(["source_event_id", "sequence", "duplicate", "event_id", "usage_record_id"]);
    if (
      Object.keys(raw).some((key) => !allowedKeys.has(key)) ||
      !Object.prototype.hasOwnProperty.call(raw, "source_event_id") ||
      !Object.prototype.hasOwnProperty.call(raw, "sequence") ||
      !Object.prototype.hasOwnProperty.call(raw, "duplicate")
    ) throw new Error("pi_gateway_event_batch_receipt_invalid");
    if (
      typeof raw.source_event_id !== "string" || !/^[A-Za-z0-9._:-]{1,160}$/.test(raw.source_event_id) ||
      !Number.isInteger(raw.sequence) || (raw.sequence as number) < 1 ||
      (raw.sequence as number) > 10_000_000 || typeof raw.duplicate !== "boolean"
    ) throw new Error("pi_gateway_event_batch_receipt_invalid");
    const suffix = raw.source_event_id.slice(raw.source_event_id.lastIndexOf(":") + 1);
    if (Number(suffix) !== raw.sequence) throw new Error("pi_gateway_event_batch_receipt_invalid");
    for (const key of ["event_id", "usage_record_id"] as const) {
      if (key in raw && (typeof raw[key] !== "string" || !raw[key] || raw[key].length > 64)) {
        throw new Error("pi_gateway_event_batch_receipt_invalid");
      }
    }
    return raw as unknown as PiGatewaySourceEventReceipt;
  });
  if (
    receipts.length < 1 ||
    receipts[receipts.length - 1].sequence !== lastSequence ||
    new Set(receipts.map((item) => item.source_event_id.slice(0, item.source_event_id.lastIndexOf(":")))).size !== 1 ||
    receipts.some((item, index) => index > 0 && item.sequence !== receipts[index - 1].sequence + 1)
  ) {
    throw new Error("pi_gateway_event_batch_receipt_invalid");
  }
  return { receipts, last_acked_source_sequence: lastSequence as number };
}

/** Runtime validation mirror for the strict FastAPI claim response DTO. */
export function parsePiGatewayClaimResponse(value: unknown): PiGatewayClaimResponse {
  if (!isRecord(value) || !exactKeys(value, ["run_id", "attempt_id", "lease_token", "lease_expires_at", "runtime_snapshot", "transcript", "secret_envelope", "adapter_catalog", "internal_tools"])) {
    throw new Error("pi_gateway_claim_response_invalid");
  }
  const envelope = value.secret_envelope;
  if (
    typeof value.run_id !== "string" || value.run_id.length < 1 || value.run_id.length > 64 ||
    typeof value.attempt_id !== "string" || value.attempt_id.length < 1 || value.attempt_id.length > 64 ||
    typeof value.lease_token !== "string" || value.lease_token.length < 32 || value.lease_token.length > 512 ||
    typeof value.lease_expires_at !== "number" || !Number.isFinite(value.lease_expires_at) || value.lease_expires_at <= 0 ||
    !isRecord(value.runtime_snapshot) || !Array.isArray(value.transcript) || value.transcript.length > 100 ||
    !isRecord(envelope) || !exactKeys(envelope, ["alg", "nonce", "ciphertext"]) ||
    envelope.alg !== "AES-256-GCM" || typeof envelope.nonce !== "string" || envelope.nonce.length < 16 || envelope.nonce.length > 64 ||
    typeof envelope.ciphertext !== "string" || envelope.ciphertext.length < 16 || envelope.ciphertext.length > 200_000 ||
    !Array.isArray(value.adapter_catalog) ||
    !Array.isArray(value.internal_tools) || value.internal_tools.length > 64
  ) throw new Error("pi_gateway_claim_response_invalid");
  try {
    assertAdapterCatalogBounds(value.adapter_catalog);
  } catch {
    throw new Error("pi_gateway_claim_response_invalid");
  }
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
    hasSensitiveKey(value.runtime_snapshot, RUNTIME_SNAPSHOT_SENSITIVE_KEYS)
  ) throw new Error("pi_gateway_claim_response_invalid");
  return value as unknown as PiGatewayClaimResponse;
}

export type PiSdkEvent =
  | { type: "session_start" }
  | { type: "user_prompt"; content: string }
  | { type: "sdk_event"; eventType: string; event?: unknown }
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
  /** Per-Run provider dispatch budget (hard-enforced before any HTTP). */
  modelBudget?: import("./model-request-budget.js").ModelRequestBudget;
  /** Effective retry configuration proof: both layers must be disabled. */
  retrySettings?(): {
    agent: { enabled: boolean; maxRetries: number; baseDelayMs: number };
    provider: { timeoutMs?: number; maxRetries?: number; maxRetryDelayMs: number };
  };
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
