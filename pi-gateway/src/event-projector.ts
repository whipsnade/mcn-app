import type { PiGatewaySourceEvent } from "./protocol.js";

type RecordValue = Record<string, unknown>;

export interface UsageProjectorDiagnostics {
  unknownEvents: number;
  invalidUsage: number;
  duplicateUsage: number;
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

export class PiSdkUsageProjector {
  private sequence = 1;
  private readonly seen = new Set<string>();
  readonly diagnostics: UsageProjectorDiagnostics = {
    unknownEvents: 0,
    invalidUsage: 0,
    duplicateUsage: 0,
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
      this.diagnostics.unknownEvents += 1;
      return undefined;
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
