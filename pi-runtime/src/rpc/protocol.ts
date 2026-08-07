export type PiRpcRecord = { type: string; [key: string]: unknown };

export function parseRpcLine(line: string): PiRpcRecord {
  const normalized = line.endsWith("\r") ? line.slice(0, -1) : line;

  if (!normalized.trim()) {
    throw new Error("empty_rpc_record");
  }

  const value: unknown = JSON.parse(normalized);
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("invalid_rpc_record");
  }

  if (typeof (value as { type?: unknown }).type !== "string") {
    throw new Error("invalid_rpc_record");
  }

  return value as PiRpcRecord;
}
