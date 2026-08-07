/**
 * 透明 DataTap MCP adapter：只做原样转发 + 审计旁路。
 *
 * 规则（Task 4 简报）：
 * - 工具名、参数、原始成功结果、错误、空结果、超时必须原样给 Pi；
 * - 只允许在返回给 Pi 的对象顶层新增独立 `_runtime_metadata`；
 * - 不自动重试、拆分、改参、换工具、熔断、意图路由；
 * - 每次工具调用只发起一次 MCP call。
 *
 * 本模块不持有任何 token；token 只存在于 HTTP client 的 Authorization 头。
 */

export interface DatatapToolDescriptor {
  name: string;
  description?: string;
  inputSchema?: unknown;
}

export interface McpCallOutcome {
  content: unknown;
  isError: boolean;
  error?: string;
  upstreamRequestId?: string;
}

/** 抽象 MCP 客户端，便于测试与真实 DataTap 实现共用一个接口。 */
export interface McpToolClient {
  listTools(): Promise<DatatapToolDescriptor[]>;
  callTool(name: string, args: Record<string, unknown>): Promise<McpCallOutcome>;
}

/** 内部审计旁路客户端（后端 internal API）。 */
export interface ToolAuditClient {
  startToolCall(call: {
    toolCallId: string;
    toolName: string;
    arguments: Record<string, unknown>;
  }): Promise<{ trackedCallId: string }>;
  settleToolCall(trackedCallId: string, rawPayload: unknown): Promise<{ evidenceId?: string }>;
  failToolCall(trackedCallId: string, error: unknown): Promise<unknown>;
}

export interface RuntimeMetadata {
  toolCallId: string;
  trackedCallId: string;
  evidenceId?: string;
  isError: boolean;
  upstreamRequestId?: string;
  error?: string;
}

export async function discoverDatatapTools(mcp: McpToolClient): Promise<DatatapToolDescriptor[]> {
  return mcp.listTools();
}

export async function callDatatapTransparent(opts: {
  mcp: McpToolClient;
  audit: ToolAuditClient;
  toolCallId: string;
  toolName: string;
  arguments: Record<string, unknown>;
  redactAudit?: (value: unknown) => unknown;
}): Promise<{ payload: unknown; metadata: RuntimeMetadata }> {
  const { mcp, audit, toolCallId, toolName } = opts;
  const argumentsValue = opts.arguments;

  const started = await audit.startToolCall({
    toolCallId,
    toolName,
    arguments: argumentsValue,
  });
  const trackedCallId = started.trackedCallId;

  const redactAudit = opts.redactAudit ?? ((value: unknown) => value);
  let outcome: McpCallOutcome;
  try {
    outcome = await mcp.callTool(toolName, argumentsValue);
  } catch (error) {
    await audit.failToolCall(trackedCallId, redactAudit({ error }));
    throw error;
  }

  const base: RuntimeMetadata = {
    toolCallId,
    trackedCallId,
    isError: outcome.isError,
    upstreamRequestId: outcome.upstreamRequestId,
  };

  if (outcome.isError) {
    const error = outcome.error ?? String(outcome.content);
    base.error = error;
    await audit.failToolCall(trackedCallId, redactAudit({ error }));
    return { payload: withRuntimeMetadata(outcome.content, base), metadata: base };
  }

  const settled = await audit.settleToolCall(trackedCallId, outcome.content);
  base.evidenceId = settled.evidenceId;
  return { payload: withRuntimeMetadata(outcome.content, base), metadata: base };
}

/** 仅新增顶层 `_runtime_metadata`；业务对象原样保留。 */
export function withRuntimeMetadata(payload: unknown, metadata: RuntimeMetadata): unknown {
  if (payload !== null && typeof payload === "object" && !Array.isArray(payload)) {
    return { ...(payload as Record<string, unknown>), _runtime_metadata: metadata };
  }
  return { result: payload, _runtime_metadata: metadata };
}
