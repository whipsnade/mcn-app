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
    return isRecord(message) && isRecord(message.usage) ? message.usage : {};
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
    if (isRecord(event.message) && isRecord(event.message.usage)) return event.message.usage;
    return {};
  }
  return undefined;
}

function eventIdentity(event: RecordValue, usage: RecordValue): string | undefined {
  const requestId = safeString(usage.requestId ?? usage.upstream_request_id);
  if (requestId) return `request:${requestId}`;
  for (const key of ["eventId", "event_id", "messageId", "message_id", "turnId", "turn_id", "id"]) {
    const value = safeString(event[key] ?? usage[key]);
    if (value) return `event:${value}`;
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

function genericProjection(event: RecordValue): { event_type: string; payload: RecordValue; identity?: string } | undefined {
  const type = event.type;
  if (type === "agent_start" || type === "turn_start") {
    return { event_type: "run.started", payload: {}, identity: safeEventId(event) };
  }
  if (type === "agent_end" || type === "turn_end") {
    return { event_type: "turn.completed", payload: {}, identity: safeEventId(event) };
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
    if (["done", "text_end", "error"].includes(String(update.type))) {
      const text = safeDelta(update.text ?? update.content);
      return { event_type: "message.completed", payload: text ? { text } : {}, identity: safeEventId(event) };
    }
    return undefined;
  }
  if (type === "message_start") return { event_type: "message.started", payload: {}, identity: safeEventId(event) };
  if (type === "tool_execution_start" || type === "tool_call") {
    const callId = safeString(event.toolCallId ?? event.callId ?? event.call_id);
    const toolName = safeString(event.toolName ?? event.tool_name ?? event.name);
    if (!callId || !toolName) return undefined;
    return {
      event_type: "tool.started",
      payload: { call_id: callId, internal_tool_name: toolName },
      identity: safeEventId(event),
    };
  }
  if (type === "tool_execution_end" || type === "tool_result") {
    const callId = safeString(event.toolCallId ?? event.callId ?? event.call_id);
    if (!callId) return undefined;
    const status = event.isError === true || event.status === "failed" ? "failed" : "succeeded";
    return { event_type: `tool.${status}`, payload: { call_id: callId, status }, identity: safeEventId(event) };
  }
  return undefined;
}

export class PiSdkUsageProjector {
  private sequence = 1;
  private readonly seen = new Set<string>();
  readonly diagnostics: UsageProjectorDiagnostics = {
    unknownEvents: 0,
    invalidUsage: 0,
    duplicateUsage: 0,
    projectedEvents: 0,
  };

  constructor(private readonly attemptId: string) {
    if (!/^[A-Za-z0-9._-]{1,64}$/.test(attemptId)) throw new Error("pi_usage_attempt_invalid");
  }

  project(event: unknown): PiGatewaySourceEvent | undefined {
    if (!isRecord(event) || typeof event.type !== "string") {
      this.diagnostics.unknownEvents += 1;
      return undefined;
    }
    const usage = usageObject(event);
    if (usage === undefined) {
      const generic = genericProjection(event);
      if (!generic) {
        this.diagnostics.unknownEvents += 1;
        return undefined;
      }
      if (generic.identity && this.seen.has(`event:${generic.identity}`)) {
        this.diagnostics.duplicateUsage += 1;
        return undefined;
      }
      if (generic.identity) this.seen.add(`event:${generic.identity}`);
      const sequence = this.sequence++;
      this.diagnostics.projectedEvents += 1;
      return {
        source_event_id: `${this.attemptId}:${sequence}`,
        sequence,
        event_type: generic.event_type,
        payload: generic.payload,
      };
    }
    const requestId = safeString(usage.requestId ?? usage.upstream_request_id);
    const dedupeKey = eventIdentity(event, usage);
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
    const sequence = this.sequence++;
    return {
      source_event_id: `${this.attemptId}:${sequence}`,
      sequence,
      event_type: "usage",
      payload: fields,
    };
  }
}

export function projectPiSdkEvent(
  event: unknown,
  projector: PiSdkUsageProjector,
): PiGatewaySourceEvent | undefined {
  return projector.project(event);
}
