import { createHash } from "node:crypto";

export const PROVIDER_FAILURE_VERSION = "provider_failure_v1" as const;

export const PROVIDER_FAILURE_CLASSES = Object.freeze([
  "authentication",
  "authorization",
  "rate_limited",
  "model_not_found",
  "invalid_request",
  "context_length",
  "timeout",
  "network",
  "upstream_5xx",
  "aborted",
  "unknown",
] as const);

export type ProviderFailureClass = (typeof PROVIDER_FAILURE_CLASSES)[number];

export interface ProviderFailureMetadata {
  version: typeof PROVIDER_FAILURE_VERSION;
  failure_class: ProviderFailureClass;
  http_status?: number;
  provider_request_id?: string;
  error_fingerprint: string;
  observed_at?: string;
}

const SAFE_REQUEST_ID = /^[A-Za-z0-9](?:[A-Za-z0-9._:-]{0,127})$/;
const SAFE_TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/;
const FINGERPRINT = /^[0-9a-fA-F]{64}$/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function exactKeys(value: Record<string, unknown>, allowed: readonly string[]): boolean {
  const allowedKeys = new Set(allowed);
  return Object.keys(value).every((key) => allowedKeys.has(key));
}

function safeRequestId(value: unknown): value is string {
  if (typeof value !== "string" || !SAFE_REQUEST_ID.test(value)) return false;
  const lower = value.toLowerCase();
  if (["bearer", "token", "secret", "api_key", "apikey", "sk-", "sk_"]
    .some((marker) => lower.startsWith(marker))) return false;
  return true;
}

function safeObservedAt(value: unknown): value is string {
  if (typeof value !== "string" || !SAFE_TIMESTAMP.test(value)) return false;
  const parsed = new Date(value);
  return Number.isFinite(parsed.getTime()) && parsed.toISOString() === value;
}

function metadataBytes(value: ProviderFailureMetadata): number {
  return new TextEncoder().encode(JSON.stringify(value)).byteLength;
}

/** Strict validator used at the child IPC and Gateway terminal boundaries. */
export function parseProviderFailureMetadata(value: unknown): ProviderFailureMetadata {
  if (!isRecord(value) || !exactKeys(value, [
    "version",
    "failure_class",
    "http_status",
    "provider_request_id",
    "error_fingerprint",
    "observed_at",
  ])) {
    throw new Error("pi_provider_failure_metadata_invalid");
  }
  if (
    value.version !== PROVIDER_FAILURE_VERSION ||
    typeof value.failure_class !== "string" ||
    !(PROVIDER_FAILURE_CLASSES as readonly string[]).includes(value.failure_class) ||
    ("http_status" in value && value.http_status !== undefined && (
      typeof value.http_status !== "number" ||
      !Number.isInteger(value.http_status) ||
      value.http_status < 100 ||
      value.http_status > 599
    )) ||
    ("provider_request_id" in value && value.provider_request_id !== undefined && !safeRequestId(value.provider_request_id)) ||
    typeof value.error_fingerprint !== "string" ||
    !FINGERPRINT.test(value.error_fingerprint) ||
    ("observed_at" in value && value.observed_at !== undefined && !safeObservedAt(value.observed_at))
  ) {
    throw new Error("pi_provider_failure_metadata_invalid");
  }
  const metadata: ProviderFailureMetadata = {
    version: PROVIDER_FAILURE_VERSION,
    failure_class: value.failure_class as ProviderFailureClass,
    error_fingerprint: value.error_fingerprint,
    ...(value.http_status === undefined ? {} : { http_status: value.http_status as number }),
    ...(value.provider_request_id === undefined ? {} : { provider_request_id: value.provider_request_id as string }),
    ...(value.observed_at === undefined ? {} : { observed_at: value.observed_at as string }),
  };
  if (metadataBytes(metadata) > 2_048) throw new Error("pi_provider_failure_metadata_invalid");
  return metadata;
}

export function isSafeProviderFailureMetadata(value: unknown): value is ProviderFailureMetadata {
  try {
    parseProviderFailureMetadata(value);
    return true;
  } catch {
    return false;
  }
}

function extractHttpStatus(errorMessage: string): number | undefined {
  const matches = [
    /(?:status(?:_code)?|status\s+code|http(?:\s+status)?|response\s+code)\s*(?:is|=|:)?\s*([1-5]\d{2})\b/i,
    /\bHTTP(?:\/\d+(?:\.\d+)?)?\s+([1-5]\d{2})\b/i,
    /^\s*([1-5]\d{2})(?=\s|$|[:;,\)\]}])/,
  ];
  for (const pattern of matches) {
    const match = pattern.exec(errorMessage);
    if (match) return Number(match[1]);
  }
  return undefined;
}

function extractProviderRequestId(errorMessage: string): string | undefined {
  const match = /(?:x[-_ ]?request[-_ ]?id|request[-_ ]?id|requestid|trace[-_ ]?id|correlation[-_ ]?id)\s*["']?\s*[:=]\s*["']?([A-Za-z0-9][A-Za-z0-9._:-]{0,127})/i.exec(errorMessage);
  const candidate = match?.[1];
  return safeRequestId(candidate) ? candidate : undefined;
}

function classifyFailure(errorMessage: string, stopReason: unknown, httpStatus: number | undefined): ProviderFailureClass {
  if (stopReason === "aborted") return "aborted";
  if (httpStatus === 401) return "authentication";
  if (httpStatus === 403) return "authorization";
  if (httpStatus === 404) return "model_not_found";
  if (httpStatus === 408) return "timeout";
  if (httpStatus === 429) return "rate_limited";
  if (httpStatus === 400 || httpStatus === 422) return "invalid_request";
  if (httpStatus !== undefined && httpStatus >= 500) return "upstream_5xx";

  const lower = errorMessage.toLowerCase();
  if (/context[_ -]?length|context window|maximum context|too many tokens|prompt (?:is )?too long|input (?:is )?too long|token limit/.test(lower)) {
    return "context_length";
  }
  if (/timeout|timed out|timedout|etimedout|deadline exceeded/.test(lower)) return "timeout";
  if (/econnrefused|econnreset|enotfound|fetch failed|network (?:error|failure|unreachable|unavailable)|socket (?:error|closed|hang up)|connection (?:reset|closed|failed|refused)/.test(lower)) {
    return "network";
  }
  if (/model(?:_name)?\s*(?:not found|does not exist|unknown)/.test(lower)) return "model_not_found";
  if (/unauthori[sz]ed|invalid api key|authentication failed/.test(lower)) return "authentication";
  if (/forbidden|not allowed|permission denied/.test(lower)) return "authorization";
  if (/rate limit|too many requests|throttl/.test(lower)) return "rate_limited";
  if (/invalid request|bad request|unprocessable entity/.test(lower)) return "invalid_request";
  if (/bad gateway|gateway timeout|service unavailable|upstream (?:error|failure|unavailable|timeout)|overloaded/.test(lower)) {
    return "upstream_5xx";
  }
  return "unknown";
}

function readErrorMessage(message: unknown): string {
  return isRecord(message) && typeof message.errorMessage === "string" ? message.errorMessage : "";
}

/**
 * Extracts only bounded metadata from an SDK AssistantMessage.  The source
 * errorMessage is used for classification and hashing in memory, never copied
 * into the returned DTO or any Error message.
 */
export function buildProviderFailureMetadata(
  message: unknown,
  observedAt: Date = new Date(),
): ProviderFailureMetadata {
  const errorMessage = readErrorMessage(message);
  const httpStatus = extractHttpStatus(errorMessage);
  const providerRequestId = extractProviderRequestId(errorMessage);
  const timestamp = Number.isFinite(observedAt.getTime()) ? observedAt.toISOString() : undefined;
  return parseProviderFailureMetadata({
    version: PROVIDER_FAILURE_VERSION,
    failure_class: classifyFailure(errorMessage, isRecord(message) ? message.stopReason : undefined, httpStatus),
    ...(httpStatus === undefined ? {} : { http_status: httpStatus }),
    ...(providerRequestId === undefined ? {} : { provider_request_id: providerRequestId }),
    error_fingerprint: createHash("sha256").update(errorMessage, "utf8").digest("hex"),
    ...(timestamp === undefined ? {} : { observed_at: timestamp }),
  });
}

export function stripProviderErrorMessage(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stripProviderErrorMessage);
  if (!isRecord(value)) return value;
  const output: Record<string, unknown> = {};
  for (const [key, item] of Object.entries(value)) {
    if (key.toLowerCase() === "errormessage") continue;
    output[key] = stripProviderErrorMessage(item);
  }
  return output;
}
