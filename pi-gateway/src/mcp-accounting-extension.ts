/**
 * Pi's MCP accounting hook is deliberately a sideband observer.
 *
 * The standard adapter result is owned by Pi and must reach the model without
 * being parsed, normalized, classified, or replaced by accounting.  Only
 * small, non-business metadata crosses the control plane for settlement.
 */

import type { ExtensionFactory, ToolCallEvent, ToolResultEvent } from "@earendil-works/pi-coding-agent";

export interface McpToolCallInput {
  tool: string;
  server: string;
  args: Record<string, unknown>;
}

export interface McpToolBinding {
  toolName: string;
  server: string;
  remoteName?: string;
}

export interface McpPermit {
  permit_id: string;
  [key: string]: unknown;
}

export interface McpBlocked {
  block: true;
  reason: string;
}

export interface McpFree {
  free: true;
}

export interface McpFinalizeMetadata {
  outcome: "succeeded";
  upstream_request_id?: string;
  response_bytes?: number;
  adapter_version?: string;
  completed_at?: string;
  response_hash?: string;
}

export type UnknownMcpSource =
  | "call_failed"
  | "aborted"
  | "worker_rpc_timeout"
  | "worker_rpc_disconnected"
  | "finalize_ack_unknown"
  | "other";

export interface McpFailureMetadata {
  version: "mcp_failure_v1";
  source: UnknownMcpSource;
  /** Metadata-only observability (commit 3). Classification is unchanged. */
  error_class?: string;
  received_jsonrpc_response?: boolean;
  dispatch_phase?: "preflight" | "dispatched" | "unknown";
  is_standard_mcp_error?: boolean;
  upstream_request_id?: string;
}

export interface McpAccountingControlPlane {
  preflight(input: McpToolCallInput): Promise<McpPermit | McpBlocked>;
  finalize(permit: McpPermit, metadata: McpFinalizeMetadata): Promise<unknown>;
  fail(
    permit: McpPermit,
    classification: "definitely_not_sent" | "failed_confirmed" | "result_unknown",
    metadata?: McpFailureMetadata,
  ): Promise<unknown>;
}

const FREE_DISCOVERY_TOOLS = new Set(["connect", "search", "list"]);

/**
 * Per-server dispatch gate tuning（非密钥）。默认并发 1、有界等待 300s；
 * 等待超时发生在 preflight 之前——无 ToolCall 行、无预留、零计费。
 */
export interface McpDispatchGateOptions {
  /** 同一 server 允许的最大在飞调用数（默认 1）。 */
  maxInflight?: number;
  /** 槽位有界等待上限（毫秒，默认 300_000）。 */
  slotWaitMs?: number;
}

const DEFAULT_MAX_INFLIGHT = 1;
const DEFAULT_SLOT_WAIT_MS = 300_000;
const MAX_INFLIGHT_LIMIT = 32;
const SLOT_WAIT_LIMIT_MS = 600_000;

/** 从非密钥调优 env 键读取闸门配置（装配时读一次；非法值回退默认）。 */
export function readMcpDispatchGateOptions(env: NodeJS.ProcessEnv): McpDispatchGateOptions {
  const inflight = Number.parseInt(env.PI_MCP_SERVER_MAX_INFLIGHT ?? "", 10);
  const wait = Number.parseInt(env.PI_MCP_SLOT_WAIT_MS ?? "", 10);
  return {
    ...(Number.isSafeInteger(inflight) && inflight >= 1 && inflight <= MAX_INFLIGHT_LIMIT
      ? { maxInflight: inflight }
      : {}),
    ...(Number.isSafeInteger(wait) && wait >= 0 && wait <= SLOT_WAIT_LIMIT_MS
      ? { slotWaitMs: wait }
      : {}),
  };
}

interface PerServerDispatchGate {
  acquire(server: string): Promise<boolean>;
  release(server: string): void;
}

/**
 * 进程内 per-server 串行闸：排队而不是拒绝，不同 server 互不阻塞。
 * 等待超时的调用者收到 false（调用方在 preflight 之前短路，零计费）。
 */
export function createPerServerDispatchGate(
  options: McpDispatchGateOptions = {},
): PerServerDispatchGate {
  const maxInflight = Math.min(
    MAX_INFLIGHT_LIMIT,
    Math.max(1, options.maxInflight ?? DEFAULT_MAX_INFLIGHT),
  );
  const waitMs = Math.min(
    SLOT_WAIT_LIMIT_MS,
    Math.max(0, options.slotWaitMs ?? DEFAULT_SLOT_WAIT_MS),
  );
  const inflight = new Map<string, number>();
  const waiters: Array<{
    server: string;
    resolve: (granted: boolean) => void;
    timer: ReturnType<typeof setTimeout>;
  }> = [];

  const acquire = (server: string): Promise<boolean> =>
    new Promise<boolean>((resolve) => {
      const current = inflight.get(server) ?? 0;
      if (current < maxInflight) {
        inflight.set(server, current + 1);
        resolve(true);
        return;
      }
      const waiter = {
        server,
        resolve,
        timer: setTimeout(() => {
          const index = waiters.indexOf(waiter);
          if (index >= 0) waiters.splice(index, 1);
          resolve(false);
        }, waitMs),
      };
      // 有界等待计时不得阻止 worker 进程退出。
      waiter.timer.unref?.();
      waiters.push(waiter);
    });

  const release = (server: string): void => {
    const index = waiters.findIndex((waiter) => waiter.server === server);
    if (index >= 0) {
      const [waiter] = waiters.splice(index, 1);
      clearTimeout(waiter.timer);
      // 槽位直接转移给队首等待者，inflight 计数保持不变。
      waiter.resolve(true);
      return;
    }
    const current = inflight.get(server) ?? 0;
    const next = Math.max(0, current - 1);
    if (next === 0) inflight.delete(server);
    else inflight.set(server, next);
  };

  return { acquire, release };
}
const UNKNOWN_MCP_SOURCES = new Set<UnknownMcpSource>([
  "call_failed",
  "aborted",
  "worker_rpc_timeout",
  "worker_rpc_disconnected",
  "finalize_ack_unknown",
  "other",
]);
const SAFE_ID = /^[A-Za-z0-9._:-]{1,128}$/;
const SAFE_VERSION = /^[A-Za-z0-9._:-]{1,64}$/;
const SAFE_TIMESTAMP = /^[A-Za-z0-9:+.TZ_-]{1,64}$/;
const SAFE_HASH = /^sha256:[0-9a-fA-F]{64}$/;
const MAX_RESPONSE_BYTES = 64 * 1024 * 1024;

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function exactKeys(value: Record<string, unknown>, expected: readonly string[]): boolean {
  const keys = Object.keys(value);
  return keys.length === expected.length && expected.every((key) => Object.prototype.hasOwnProperty.call(value, key));
}

function optionalString(value: unknown, pattern: RegExp): boolean {
  return value === undefined || (typeof value === "string" && pattern.test(value));
}

/** Validate the exact metadata envelope before it can cross IPC. */
export function isSafeMcpFinalizeMetadata(value: unknown): value is McpFinalizeMetadata {
  if (!isRecord(value) || !Object.prototype.hasOwnProperty.call(value, "outcome")) return false;
  const allowed = [
    "outcome",
    "upstream_request_id",
    "response_bytes",
    "adapter_version",
    "completed_at",
    "response_hash",
  ] as const;
  if (Object.keys(value).some((key) => !allowed.includes(key as (typeof allowed)[number]))) return false;
  if (
    value.outcome !== "succeeded"
    || !optionalString(value.upstream_request_id, SAFE_ID)
    || !optionalString(value.adapter_version, SAFE_VERSION)
    || !optionalString(value.completed_at, SAFE_TIMESTAMP)
    || !optionalString(value.response_hash, SAFE_HASH)
  ) return false;
  return value.response_bytes === undefined
    || (typeof value.response_bytes === "number"
      && Number.isSafeInteger(value.response_bytes)
      && value.response_bytes >= 0
      && value.response_bytes <= MAX_RESPONSE_BYTES);
}

export function isSafeMcpFailureMetadata(value: unknown): value is McpFailureMetadata {
  if (!isRecord(value)) return false;
  const allowed = [
    "version",
    "source",
    "error_class",
    "received_jsonrpc_response",
    "dispatch_phase",
    "is_standard_mcp_error",
    "upstream_request_id",
  ] as const;
  if (Object.keys(value).some((key) => !allowed.includes(key as (typeof allowed)[number]))) return false;
  return value.version === "mcp_failure_v1"
    && typeof value.source === "string"
    && UNKNOWN_MCP_SOURCES.has(value.source as UnknownMcpSource)
    && (value.error_class === undefined || (typeof value.error_class === "string" && value.error_class.length <= 64))
    && (value.received_jsonrpc_response === undefined || typeof value.received_jsonrpc_response === "boolean")
    && (value.dispatch_phase === undefined
      || value.dispatch_phase === "preflight"
      || value.dispatch_phase === "dispatched"
      || value.dispatch_phase === "unknown")
    && (value.is_standard_mcp_error === undefined || typeof value.is_standard_mcp_error === "boolean")
    && (value.upstream_request_id === undefined || (typeof value.upstream_request_id === "string" && value.upstream_request_id.length <= 128));
}

function firstRecord(...values: unknown[]): Record<string, unknown> | undefined {
  return values.find(isRecord);
}

function safeString(value: unknown, pattern: RegExp): string | undefined {
  return typeof value === "string" && pattern.test(value) ? value : undefined;
}

function safeInteger(value: unknown): number | undefined {
  return typeof value === "number"
    && Number.isSafeInteger(value)
    && value >= 0
    && value <= MAX_RESPONSE_BYTES
    ? value
    : undefined;
}

function requestIdFrom(details: Record<string, unknown>): string | undefined {
  const containers = [details, details._meta, details.meta];
  for (const container of containers) {
    if (!isRecord(container)) continue;
    const value = safeString(container.upstream_request_id, SAFE_ID)
      ?? safeString(container.request_id, SAFE_ID)
      ?? safeString(container.requestId, SAFE_ID);
    if (value !== undefined) return value;
  }
  return undefined;
}

/**
 * Copy only explicitly approved accounting metadata.  In particular this
 * function never receives or inspects `event.content`.
 */
export function buildMcpFinalizeMetadata(details: Record<string, unknown>): McpFinalizeMetadata {
  const meta = firstRecord(details._meta, details.meta);
  const responseBytes = safeInteger(
    details.response_bytes
      ?? details.responseByteCount
      ?? meta?.response_bytes
      ?? meta?.responseByteCount,
  );
  const adapterVersion = safeString(
    details.adapter_version ?? details.adapterVersion ?? meta?.adapter_version ?? meta?.adapterVersion,
    SAFE_VERSION,
  );
  const completedAt = safeString(
    details.completed_at ?? details.completedAt ?? meta?.completed_at ?? meta?.completedAt,
    SAFE_TIMESTAMP,
  );
  const responseHash = safeString(
    details.response_hash ?? details.responseHash ?? meta?.response_hash ?? meta?.responseHash,
    SAFE_HASH,
  );
  const metadata: McpFinalizeMetadata = {
    outcome: "succeeded",
    ...(requestIdFrom(details) === undefined ? {} : { upstream_request_id: requestIdFrom(details) }),
    ...(responseBytes === undefined ? {} : { response_bytes: responseBytes }),
    ...(adapterVersion === undefined ? {} : { adapter_version: adapterVersion }),
    ...(completedAt === undefined ? {} : { completed_at: completedAt }),
    ...(responseHash === undefined ? {} : { response_hash: responseHash }),
  };
  // The constructor above uses only bounded values; retain a defensive check
  // so a future metadata addition cannot silently widen the control plane.
  return isSafeMcpFinalizeMetadata(metadata) ? metadata : { outcome: "succeeded" };
}

/**
 * Adapter error codes that prove the call never left the local process.  They
 * release a reservation without pretending that a supplier failure occurred.
 */
const NO_DISPATCH_ERROR_CODES: ReadonlySet<string> = new Set([
  "server_backoff",
  "connect_failed",
  "not_connected",
  "server_not_connected",
  "auth_required",
  "not_authenticated",
  "not_initialized",
  "init_failed",
  "init_timeout",
  "server_disabled",
  "server_unavailable",
  "server_not_found",
  "missing_server",
  "tool_not_found",
  "tool_not_found_after_reconnect",
  "auth_start_failed",
  "auth_complete_failed",
  "oauth_not_supported",
  "url_elicitation_required",
  "unsafe_pattern",
  "invalid_pattern",
  "query_too_long",
  "empty_query",
  "missing_input",
]);

type FailureClassification = "definitely_not_sent" | "failed_confirmed" | "result_unknown";

export function classifyMcpFailure(
  errorCode: string | undefined,
  details: Record<string, unknown>,
  isError = false,
): FailureClassification {
  // 冻结合同（按序短路）：响应确认信号（SDK isError = 收到标准 MCP error
  // 响应）优先于 adapter error code——供应商已确认返回错误响应的调用按
  // failed_confirmed 收口释放，只有无响应信号时才 fail-safe 到 result_unknown。
  if (errorCode === "tool_error") return "failed_confirmed";
  if (errorCode !== undefined && NO_DISPATCH_ERROR_CODES.has(errorCode)) return "definitely_not_sent";
  if (isError) return "failed_confirmed";
  const explicit = details.classification;
  if (
    explicit === "definitely_not_sent"
    || explicit === "failed_confirmed"
    || explicit === "result_unknown"
  ) return explicit;
  if (errorCode === "call_failed") return "result_unknown";
  return "result_unknown";
}

function unknownMcpSource(error: unknown, fallback: UnknownMcpSource = "other"): UnknownMcpSource {
  const message = error instanceof Error ? error.message : undefined;
  if (message === "call_failed" || message === "aborted") return message;
  if (message === "worker_rpc_timeout") return "worker_rpc_timeout";
  if (message === "worker_rpc_disconnected") return "worker_rpc_disconnected";
  return fallback;
}

export function proxyVisibleToolName(server: string, remoteName: string): string {
  return `${server.replace(/-/g, "_")}_${remoteName.replace(/\./g, "_")}`;
}

export class McpAccountingExtension {
  constructor(private readonly controlPlane: McpAccountingControlPlane) {}

  async beforeToolCall(input: McpToolCallInput): Promise<McpPermit | McpBlocked | McpFree> {
    if (FREE_DISCOVERY_TOOLS.has(input.tool)) return { free: true };
    if (!input.tool || !input.server) return { block: true, reason: "mcp_tool_identity_invalid" };
    try {
      const result = await this.controlPlane.preflight(input);
      if ("block" in result && result.block === true) return result;
      if (!("permit_id" in result) || typeof result.permit_id !== "string" || result.permit_id.length === 0) {
        return { block: true, reason: "mcp_permit_invalid" };
      }
      return result;
    } catch {
      return { block: true, reason: "control_plane_unreachable" };
    }
  }

  async afterToolResult(permit: McpPermit, metadata: McpFinalizeMetadata): Promise<unknown> {
    if (!isSafeMcpFinalizeMetadata(metadata)) throw new Error("mcp_finalize_metadata_invalid");
    return this.controlPlane.finalize(permit, metadata);
  }

  async afterToolError(
    permit: McpPermit,
    classification: FailureClassification,
    metadata?: McpFailureMetadata,
  ): Promise<unknown> {
    if (metadata !== undefined && !isSafeMcpFailureMetadata(metadata)) {
      throw new Error("mcp_failure_metadata_invalid");
    }
    return metadata === undefined
      ? this.controlPlane.fail(permit, classification)
      : this.controlPlane.fail(permit, classification, metadata);
  }
}

/**
 * Install the accounting boundary into Pi's real SDK tool hooks.  The hook
 * records only the permit identity and observes completion; it never returns
 * a replacement result and never changes `event.content`.
 */
/**
 * Copy only explicitly approved failure metadata (commit 3: metadata-only
 * observability; `classifyMcpFailure` itself is unchanged).  Fields the
 * adapter error path cannot determine reliably are omitted rather than guessed.
 * `received_jsonrpc_response` (Minor #2) is only set when the SDK `isError`
 * marker confirms a standard MCP error response was actually received; the
 * uncertain adapter-error paths omit it (never guessed as false).
 */
export function buildMcpFailureMetadata(
  source: UnknownMcpSource,
  errorCode: string | undefined,
  details: Record<string, unknown>,
  isError: boolean,
): McpFailureMetadata {
  const metadata: McpFailureMetadata = { version: "mcp_failure_v1", source };
  if (errorCode !== undefined) metadata.error_class = errorCode;
  if (errorCode === "call_failed") metadata.dispatch_phase = "dispatched";
  else if (errorCode !== undefined && NO_DISPATCH_ERROR_CODES.has(errorCode)) metadata.dispatch_phase = "preflight";
  else metadata.dispatch_phase = "unknown";
  if (isError) {
    // SDK 只在收到标准 MCP CallToolResult error 响应后置位 isError：这是
    // 「确认收到了 JSON-RPC 响应」的唯一可靠信号；其他路径不确定则不设置。
    metadata.is_standard_mcp_error = true;
    metadata.received_jsonrpc_response = true;
  }
  const upstream = requestIdFrom(details);
  if (upstream !== undefined) metadata.upstream_request_id = upstream;
  return isSafeMcpFailureMetadata(metadata) ? metadata : { version: "mcp_failure_v1", source };
}

export function createMcpAccountingExtensionFactory(
  accounting: McpAccountingExtension,
  bindings: readonly McpToolBinding[] = [],
  gateOptions: McpDispatchGateOptions = {},
): ExtensionFactory {
  // 生产从非密钥 env 调优键读取默认值；显式 options 优先（测试/注入用）。
  const gate = createPerServerDispatchGate({
    ...readMcpDispatchGateOptions(process.env),
    ...gateOptions,
  });
  return (pi) => {
    const permits = new Map<string, McpPermit>();
    // toolCallId → 占用槽位的 server；tool_result 的所有出口都经 releaseSlot
    // 释放且恰好一次，杜绝槽位泄漏。
    const slotServers = new Map<string, string>();
    const hooks = pi as unknown as {
      on(event: "tool_call", handler: (event: ToolCallEvent) => Promise<unknown>): void;
      on(event: "tool_result", handler: (event: ToolResultEvent) => Promise<unknown>): void;
    };

    hooks.on("tool_call", async (event: ToolCallEvent) => {
      const input = toMcpToolCall(event, bindings);
      if (input === undefined) return;
      if ("block" in input) return input;
      // 免费发现工具不占槽：目录读取零计费，不参与串行闸。
      if (!FREE_DISCOVERY_TOOLS.has(input.tool)) {
        const granted = await gate.acquire(input.server);
        if (!granted) {
          // 等待超时发生在 preflight 之前：无 ToolCall、无预留、零计费；
          // 模型看到可恢复结构化错误自行调整，内核不自动重试。
          return { block: true, reason: "mcp_server_busy" };
        }
      }
      const gated = !FREE_DISCOVERY_TOOLS.has(input.tool);
      try {
        const decision = await accounting.beforeToolCall(input);
        if ("block" in decision && decision.block) {
          if (gated) gate.release(input.server);
          return decision;
        }
        if ("permit_id" in decision) {
          permits.set(event.toolCallId, decision);
          if (gated) slotServers.set(event.toolCallId, input.server);
        } else if (gated) {
          gate.release(input.server);
        }
        return undefined;
      } catch (error) {
        if (gated) gate.release(input.server);
        throw error;
      }
    });

    hooks.on("tool_result", async (event: ToolResultEvent) => {
      const permit = permits.get(event.toolCallId);
      if (!permit) return;
      const server = slotServers.get(event.toolCallId);
      const releaseSlot = (): void => {
        if (server === undefined) return;
        slotServers.delete(event.toolCallId);
        gate.release(server);
      };
      const details = isRecord(event.details) ? event.details : {};
      const errorCode = typeof details.error === "string" ? details.error : undefined;
      const hasErrorMarker = Object.prototype.hasOwnProperty.call(details, "error")
        && details.error !== undefined
        && details.error !== null
        && details.error !== false;

      const failUnknown = async (source: UnknownMcpSource): Promise<boolean> => {
        try {
          await accounting.afterToolError(permit, "result_unknown",
            buildMcpFailureMetadata(source, errorCode, details, event.isError === true));
          permits.delete(event.toolCallId);
          releaseSlot();
          return true;
        } catch {
          // No durable ACK means the permit remains recoverable for backend
          // reconciliation; deleting it here would lose the reservation.
          // 槽位仍必须释放：调用本身已结束，不能占住 per-server 闸。
          releaseSlot();
          return false;
        }
      };

      try {
        if (event.isError || hasErrorMarker || errorCode !== undefined) {
          const classification = classifyMcpFailure(
            errorCode,
            details,
            event.isError === true,
          );
          try {
            // result_unknown 与 failed_confirmed 都携带 metadata-only 失败
            // 元数据；只有 definitely_not_sent（本地未外发）维持 undefined。
            await accounting.afterToolError(
              permit,
              classification,
              classification === "definitely_not_sent"
                ? undefined
                : buildMcpFailureMetadata(
                    errorCode === "call_failed" ? "call_failed" : "other",
                    errorCode,
                    details,
                    event.isError === true,
                  ),
            );
            permits.delete(event.toolCallId);
            releaseSlot();
          } catch (error) {
            await failUnknown(unknownMcpSource(error, "finalize_ack_unknown"));
          }
          return;
        }
        // Deliberately inspect only details for approved control metadata.
        // `event.content` is left untouched and is never passed to accounting.
        await accounting.afterToolResult(permit, buildMcpFinalizeMetadata(details));
        permits.delete(event.toolCallId);
        releaseSlot();
      } catch (error) {
        await failUnknown(unknownMcpSource(error, "finalize_ack_unknown"));
      }
    });
  };
}

function toMcpToolCall(
  event: ToolCallEvent,
  bindings: readonly McpToolBinding[],
): McpToolCallInput | McpBlocked | undefined {
  const input: Record<string, unknown> = isRecord(event.input) ? event.input : {};
  if (event.toolName === "mcp") {
    if (typeof input.tool !== "string" || input.tool.length === 0) return undefined;
    const args = normalizeArgs(input.args);
    const requestedServer = typeof input.server === "string" ? input.server : "";
    const candidates = bindings.filter((binding) => {
      if (requestedServer.length > 0 && binding.server !== requestedServer) return false;
      if (binding.toolName === input.tool) return true;
      if (binding.remoteName === undefined) return false;
      return binding.remoteName === input.tool
        || binding.remoteName.replace(/\./g, "_") === input.tool
        || proxyVisibleToolName(binding.server, binding.remoteName) === input.tool;
    });
    const unique = new Map<string, McpToolBinding>();
    for (const binding of candidates) unique.set(`${binding.server}\u0000${binding.toolName}`, binding);
    if (unique.size === 1) {
      const match = [...unique.values()][0];
      // Preserve the Scenario 1 bare-name identity pin: the adapter dispatches
      // with the same server that the accounting preflight reserved.
      if (requestedServer.length === 0 && isRecord(event.input)) {
        (event.input as Record<string, unknown>).server = match.server;
      }
      return { tool: match.toolName, server: match.server, args };
    }
    if (unique.size > 1) return { block: true, reason: "mcp_tool_identity_ambiguous" };
    return { block: true, reason: "mcp_tool_identity_invalid" };
  }
  const binding = bindings.find((item) => item.toolName === event.toolName);
  if (!binding) return undefined;
  return { tool: binding.toolName, server: binding.server, args: input };
}

function normalizeArgs(value: unknown): Record<string, unknown> {
  if (value === undefined) return {};
  if (typeof value === "string") {
    try {
      const parsed: unknown = JSON.parse(value);
      return isRecord(parsed) ? parsed : {};
    } catch {
      return {};
    }
  }
  return isRecord(value) ? value : {};
}
