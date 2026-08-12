import type { PiGatewaySourceEvent } from "./protocol.js";

type RecordValue = Record<string, unknown>;

export interface UsageProjectorDiagnostics {
  unknownEvents: number;
  invalidUsage: number;
  duplicateUsage: number;
  projectedEvents: number;
}

function isRecord(value: unknown): value is RecordValue {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function integer(value: unknown): number | undefined {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 && value <= 1e12
    ? value
    : undefined;
}

function safeString(value: unknown, max = 128): string | undefined {
  return typeof value === "string" && value.length > 0 && value.length <= max ? value : undefined;
}

function usageObject(event: RecordValue): RecordValue | undefined {
  if (event.type === "usage" && isRecord(event.usage)) return event.usage;
  if (event.type === "message_end" || event.type === "turn_end") {
    const message = event.message;
    return isRecord(message) && isRecord(message.usage) ? message.usage : undefined;
  }
  if (event.type === "message_update") {
    const update = event.assistantMessageEvent;
    if (!isRecord(update)) {
      return isRecord(event.message) && isRecord(event.message.usage)
        ? event.message.usage
        : undefined;
    }
    const updateType = update.type;
    if (!["done", "error", "text_end", "usage"].includes(String(updateType))) return undefined;
    if (isRecord(update.usage)) return update.usage;
    if (isRecord(update.partial) && isRecord(update.partial.usage)) return update.partial.usage;
    if (isRecord(update.message) && isRecord(update.message.usage)) return update.message.usage;
    if (isRecord(event.message) && isRecord(event.message.usage)) return event.message.usage;
    // 没有 usage 时不立即制造 unavailable：同一 provider 调用的真实 usage
    // 可能由后续 message_end/turn_end 携带；turn 边界统一兜底。
    return undefined;
  }
  return undefined;
}

function safeDelta(value: unknown): string | undefined {
  return typeof value === "string" && value.length > 0 && value.length <= 16_384
    ? value
    : undefined;
}

function safeEventId(event: RecordValue): string | undefined {
  for (const key of ["eventId", "event_id", "messageId", "message_id", "id"]) {
    const value = safeString(event[key]);
    if (value) return value;
  }
  return undefined;
}

function extractCompletionCandidate(event: RecordValue): string | undefined {
  const update = event.assistantMessageEvent;
  if (!isRecord(update)) return undefined;
  const text = safeDelta(update.text ?? update.content);
  const message = isRecord(update.message) ? update.message : undefined;
  const content = message && Array.isArray(message.content) ? message.content : undefined;
  const contentText = content
    ?.filter((block): block is RecordValue => isRecord(block))
    .filter((block) => block.type === "text")
    .map((block) => safeDelta(block.text))
    .filter((value): value is string => Boolean(value))
    .join("");
  return text ?? (contentText || undefined);
}

function genericProjection(event: RecordValue): { event_type: string; payload: RecordValue; identity?: string } | undefined {
  const type = event.type;
  if (type === "agent_start") {
    return { event_type: "agent.turn.start", payload: {}, identity: safeEventId(event) };
  }
  if (type === "turn_start") {
    // turn_start 是每轮一次的 SDK 事件，不能映射成每次会话唯一的 run.started；
    // 独立别名，后端归一为 turn.started。
    return { event_type: "turn.start", payload: {}, identity: safeEventId(event) };
  }
  if (type === "agent_end" || type === "turn_end") {
    return { event_type: "agent.turn.end", payload: {}, identity: safeEventId(event) };
  }
  if (type === "message_update") {
    const update = event.assistantMessageEvent;
    if (!isRecord(update)) return undefined;
    const delta = safeDelta(update.delta);
    if (update.type === "thinking_delta" && delta) {
      return { event_type: "thinking.delta", payload: { text: delta }, identity: safeEventId(event) };
    }
    if (update.type === "text_delta" && delta) {
      return { event_type: "message.delta", payload: { text: delta }, identity: safeEventId(event) };
    }
    // text_end/done 的文本只是 completion 候选：前导语后接工具调用时必须作废，
    // 只有 agent_end/turn_end 收口时的最近候选才发布 message.completed。
    // 该状态由 project() 持有（pendingCompletionText），此处不再产出事件。
    return undefined;
  }
  if (type === "message_start") return { event_type: "message.start", payload: {}, identity: safeEventId(event) };
  if (type === "tool_execution_start" || type === "tool_call") {
    const callId = safeString(event.toolCallId ?? event.callId ?? event.call_id);
    const toolName = safeString(event.toolName ?? event.tool_name ?? event.name);
    if (!callId || !toolName) return undefined;
    return {
      event_type: "tool.start",
      payload: { call_id: callId, internal_tool_name: toolName },
      identity: safeEventId(event),
    };
  }
  if (type === "tool_execution_end" || type === "tool_result") {
    const callId = safeString(event.toolCallId ?? event.callId ?? event.call_id);
    if (!callId) return undefined;
    const status = event.isError === true || event.status === "failed" ? "failed" : "succeeded";
    return { event_type: "tool.end", payload: { call_id: callId, status }, identity: safeEventId(event) };
  }
  return undefined;
}

export class PiSdkUsageProjector {
  private sequence = 1;
  private readonly seen = new Set<string>();
  private completionEmitted = false;
  /** 最近一次 text_end/done 的候选最终文本；工具调用出现即作废。 */
  private pendingCompletionText: string | undefined;
  /** 当前 turn 序号（turn_start 递增）与本 turn 是否已产出 usage 记录。 */
  private turnIndex = 0;
  private usageEmittedThisTurn = false;
  readonly diagnostics: UsageProjectorDiagnostics = {
    unknownEvents: 0,
    invalidUsage: 0,
    duplicateUsage: 0,
    projectedEvents: 0,
  };

  constructor(private readonly attemptId: string) {
    if (!/^[A-Za-z0-9._-]{1,64}$/.test(attemptId)) throw new Error("pi_usage_attempt_invalid");
  }

  /**
   * Project one SDK event into zero or more ordered product events.
   *
   * message.completed 只在 agent_end/turn_end 收口时发布：text_end/done
   * 只登记候选文本，后续 tool_execution_start 把它作废（文本前导 → 工具
   * 调用 → 最终回答）；候选与 delta 都按构造时的 attemptId 归属。
   * usage 事件不受影响，与 completion 同帧时 completion 仍然先出。
   */
  project(event: unknown): PiGatewaySourceEvent[] {
    if (!isRecord(event) || typeof event.type !== "string") {
      this.diagnostics.unknownEvents += 1;
      return [];
    }
    if (event.type === "agent_start") {
      this.turnIndex = 0;
      this.usageEmittedThisTurn = false;
    }
    if (event.type === "turn_start") {
      // turn_start 重置 turn 范围：不同 turn 中 token 数恰好相同的真实请求
      // 属于不同 provider 调用，绝不能跨 turn 去重。
      this.turnIndex += 1;
      this.usageEmittedThisTurn = false;
    }
    if (event.type === "tool_execution_start" || event.type === "tool_call") {
      // 工具调用意味着之前的文本只是前导语，不是最终回答。
      this.pendingCompletionText = undefined;
    }
    if (event.type === "message_update") {
      const update = event.assistantMessageEvent;
      if (isRecord(update) && (update.type === "text_end" || update.type === "done")) {
        const candidate = extractCompletionCandidate(event);
        if (candidate) this.pendingCompletionText = candidate;
      }
    }
    const isTurnBoundary = event.type === "agent_end" || event.type === "turn_end";
    const generic = genericProjection(event);
    const usage = usageObject(event);
    const out: PiGatewaySourceEvent[] = [];
    if (isTurnBoundary && this.pendingCompletionText && !this.completionEmitted) {
      // 最终回答：completion 先于 turn.end 与 usage 发布，每 Attempt 恰好一次。
      this.completionEmitted = true;
      out.push(this.nextEvent("message.completed", { text: this.pendingCompletionText }));
      this.pendingCompletionText = undefined;
    }
    if (generic !== undefined) {
      const dedupeKey = generic.identity ? `event:${generic.identity}` : undefined;
      if (dedupeKey && this.seen.has(dedupeKey)) {
        this.diagnostics.duplicateUsage += 1;
      } else {
        if (dedupeKey) this.seen.add(dedupeKey);
        out.push(this.nextEvent(generic.event_type, generic.payload));
      }
    }
    if (usage !== undefined) {
      const usageEvent = this.projectUsage(event, usage);
      if (usageEvent !== undefined) {
        this.usageEmittedThisTurn = true;
        out.push(usageEvent);
      }
    } else if (isTurnBoundary && !this.usageEmittedThisTurn) {
      // turn 收口仍无任何 usage：恰好一条 unavailable 兜底（同一 provider
      // 调用最终最多一条 RuntimeUsageRecord）。
      this.usageEmittedThisTurn = true;
      out.push(this.nextEvent("usage", { usage_status: "unavailable" }));
    }
    if (generic === undefined && usage === undefined && out.length === 0) {
      // 已知类型空产出（如无 usage 的 message_end）不算 unknown。
      const KNOWN_SILENT = new Set([
        "agent_start", "turn_start", "agent_end", "turn_end",
        "message_start", "message_update", "message_end",
        "tool_execution_start", "tool_call", "tool_execution_end", "tool_result", "usage",
      ]);
      if (!KNOWN_SILENT.has(event.type)) {
        this.diagnostics.unknownEvents += 1;
      }
    }
    return out;
  }

  private nextEvent(eventType: string, payload: RecordValue): PiGatewaySourceEvent {
    const sequence = this.sequence++;
    this.diagnostics.projectedEvents += 1;
    return {
      source_event_id: `${this.attemptId}:${sequence}`,
      sequence,
      event_type: eventType,
      payload,
    };
  }

  private assistantMessageId(event: RecordValue): string | undefined {
    const direct = isRecord(event.message) ? safeString(event.message.id) : undefined;
    if (direct) return direct;
    const update = event.assistantMessageEvent;
    if (isRecord(update) && isRecord(update.message)) return safeString(update.message.id);
    return undefined;
  }

  /** turn 内稳定 usage 指纹（仅用于无 request/message id 时的同调用去重）。 */
  private turnUsageFingerprint(usage: RecordValue): string | undefined {
    const tokens = [
      usage.input_tokens ?? usage.input,
      usage.output_tokens ?? usage.output,
      usage.cache_read_tokens ?? usage.cache_read ?? usage.cacheRead,
      usage.cache_write_tokens ?? usage.cache_write ?? usage.cacheWrite,
    ].map((value) => (integer(value) ?? "x"));
    const model = safeString(usage.model) ?? "";
    const provider = safeString(usage.provider) ?? "";
    return `t${this.turnIndex}|${tokens.join(",")}|${provider}|${model}`;
  }

  private projectUsage(event: RecordValue, usage: RecordValue): PiGatewaySourceEvent | undefined {
    const requestId = safeString(usage.requestId ?? usage.upstream_request_id);
    // 去重优先级：upstream request id → assistant message id → turn 内 usage 指纹。
    const messageId = requestId ? undefined : this.assistantMessageId(event);
    const dedupeKey = requestId
      ? `request:${requestId}`
      : messageId
        ? `usage-message:${messageId}`
        : `turn-usage:${this.turnUsageFingerprint(usage)}`;
    if (dedupeKey && this.seen.has(dedupeKey)) {
      this.diagnostics.duplicateUsage += 1;
      return undefined;
    }
    const fields: RecordValue = {};
    const aliases: Array<[string, string[]]> = [
      ["input_tokens", ["input_tokens", "input"]],
      ["output_tokens", ["output_tokens", "output"]],
      ["cache_read_tokens", ["cache_read_tokens", "cache_read", "cacheRead"]],
      ["cache_write_tokens", ["cache_write_tokens", "cache_write", "cacheWrite"]],
    ];
    for (const [target, keys] of aliases) {
      const raw = keys.map((key) => usage[key]).find((value) => value !== undefined);
      if (raw !== undefined) {
        const value = integer(raw);
        if (value === undefined) {
          this.diagnostics.invalidUsage += 1;
          return undefined;
        }
        fields[target] = value;
      }
    }
    if (requestId) fields.upstream_request_id = requestId;
    const provider = safeString(usage.provider);
    const model = safeString(usage.model);
    if (provider) fields.provider = provider;
    if (model) fields.model = model;
    fields.usage_status = Object.keys(fields).some((key) => key.endsWith("_tokens"))
      ? "available"
      : "unavailable";
    if (dedupeKey) this.seen.add(dedupeKey);
    return this.nextEvent("usage", fields);
  }
}

export function projectPiSdkEvent(
  event: unknown,
  projector: PiSdkUsageProjector,
): PiGatewaySourceEvent[] {
  return projector.project(event);
}
