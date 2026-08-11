/**
 * Typed parent/child IPC RPC for the isolated Pi worker.
 *
 * The child never receives the control-plane HMAC secret or the Run lease;
 * every internal tool call and MCP accounting step is a correlated,
 * bounded RPC that the parent executes against FastAPI on the child's
 * behalf.
 */

import type {
  McpAccountingControlPlane,
  McpBlocked,
  McpPermit,
  McpToolCallInput,
} from "./mcp-accounting-extension.js";
import type { ControlPlaneTransport } from "./control-plane-client.js";

export const WORKER_RPC_METHODS = [
  "internal_tool",
  "mcp_preflight",
  "mcp_finalize",
  "mcp_fail",
] as const;
export type WorkerRpcMethod = (typeof WORKER_RPC_METHODS)[number];

export const WORKER_RPC_MAX_REQUEST_BYTES = 64 * 1024;
/** mcp_finalize 携带完整结构化结果，独立放行到 1 MiB（后端同口径）。 */
export const WORKER_RPC_MAX_FINALIZE_BYTES = 1024 * 1024;
export const WORKER_RPC_MAX_RESPONSE_BYTES = 1024 * 1024;
const RPC_ID_PATTERN = /^[A-Za-z0-9._:-]{1,128}$/;
const SAFE_ERROR_CODE = /^[a-z0-9_:-]{1,64}$/;

export interface WorkerRpcRequest {
  type: "worker_rpc";
  id: string;
  method: WorkerRpcMethod;
  params: Record<string, unknown>;
}

export interface WorkerRpcResponse {
  type: "worker_rpc_result";
  id: string;
  ok: boolean;
  result?: unknown;
  error?: { code: string };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function exactKeys(value: Record<string, unknown>, expected: readonly string[]): boolean {
  const keys = Object.keys(value);
  return keys.length === expected.length && expected.every((key) => key in value);
}

function byteLength(value: unknown): number {
  return new TextEncoder().encode(JSON.stringify(value)).byteLength;
}

export function parseWorkerRpcRequest(value: unknown): WorkerRpcRequest {
  if (!isRecord(value) || !exactKeys(value, ["type", "id", "method", "params"])) {
    throw new Error("worker_rpc_invalid");
  }
  const maxParamBytes =
    value.method === "mcp_finalize"
      ? WORKER_RPC_MAX_FINALIZE_BYTES
      : WORKER_RPC_MAX_REQUEST_BYTES;
  if (
    value.type !== "worker_rpc" ||
    typeof value.id !== "string" ||
    !RPC_ID_PATTERN.test(value.id) ||
    typeof value.method !== "string" ||
    !(WORKER_RPC_METHODS as readonly string[]).includes(value.method) ||
    !isRecord(value.params) ||
    byteLength(value.params) > maxParamBytes
  ) {
    throw new Error("worker_rpc_invalid");
  }
  return {
    type: "worker_rpc",
    id: value.id,
    method: value.method as WorkerRpcMethod,
    params: value.params,
  };
}

export function parseWorkerRpcResponse(value: unknown): WorkerRpcResponse {
  if (!isRecord(value) || value.type !== "worker_rpc_result") {
    throw new Error("worker_rpc_invalid");
  }
  if (typeof value.id !== "string" || !RPC_ID_PATTERN.test(value.id)) {
    throw new Error("worker_rpc_invalid");
  }
  if (value.ok === true) {
    if (
      !exactKeys(value, ["type", "id", "ok", "result"]) ||
      !("result" in value) ||
      value.result === undefined
    ) {
      throw new Error("worker_rpc_invalid");
    }
    return { type: "worker_rpc_result", id: value.id, ok: true, result: value.result };
  }
  if (value.ok === false) {
    if (!exactKeys(value, ["type", "id", "ok", "error"]) || !isRecord(value.error)) {
      throw new Error("worker_rpc_invalid");
    }
    const code = value.error.code;
    if (typeof code !== "string" || code.length === 0 || code.length > 128) {
      throw new Error("worker_rpc_invalid");
    }
    return { type: "worker_rpc_result", id: value.id, ok: false, error: { code } };
  }
  throw new Error("worker_rpc_invalid");
}

/** Minimal channel surface so tests can substitute an in-memory pair. */
export interface WorkerRpcChannel {
  send(message: unknown): void;
  onMessage(listener: (message: unknown) => void): () => void;
  onDisconnect?(listener: () => void): () => void;
}

export class WorkerRpcClient {
  private readonly channel: WorkerRpcChannel;
  private readonly timeoutMs: number;
  private readonly pending = new Map<
    string,
    { resolve: (result: unknown) => void; reject: (error: Error) => void; timer: ReturnType<typeof setTimeout> }
  >();
  private disposed = false;
  private counter = 0;
  private readonly detach: () => void;

  constructor(channel: WorkerRpcChannel, options: { timeoutMs?: number } = {}) {
    this.timeoutMs = options.timeoutMs ?? 30_000;
    if (!Number.isFinite(this.timeoutMs) || this.timeoutMs < 1 || this.timeoutMs > 120_000) {
      throw new Error("worker_rpc_timeout_invalid");
    }
    this.channel = channel;
    const detachMessage = channel.onMessage((message) => this.handle(message));
    const detachDisconnect = channel.onDisconnect?.(() => this.dispose());
    this.detach = () => {
      detachMessage();
      detachDisconnect?.();
    };
  }

  get pendingCount(): number {
    return this.pending.size;
  }

  call(method: WorkerRpcMethod, params: Record<string, unknown>): Promise<unknown> {
    if (this.disposed) return Promise.reject(new Error("worker_rpc_disposed"));
    this.counter += 1;
    const id = `rpc-${this.counter}-${Math.random().toString(36).slice(2, 10)}`;
    const request: WorkerRpcRequest = { type: "worker_rpc", id, method, params };
    const maxBytes =
      method === "mcp_finalize"
        ? WORKER_RPC_MAX_FINALIZE_BYTES
        : WORKER_RPC_MAX_REQUEST_BYTES;
    if (byteLength(request) > maxBytes + 256) {
      return Promise.reject(new Error("worker_rpc_payload_too_large"));
    }
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error("worker_rpc_timeout"));
      }, this.timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      try {
        this.channel.send(request);
      } catch {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(new Error("worker_rpc_disconnected"));
      }
    });
  }

  /** Reject every pending call and stop listening; idempotent. */
  dispose(code = "worker_rpc_disposed"): void {
    if (this.disposed) return;
    this.disposed = true;
    this.detach();
    for (const entry of this.pending.values()) {
      clearTimeout(entry.timer);
      entry.reject(new Error(code));
    }
    this.pending.clear();
  }

  private handle(message: unknown): void {
    let response: WorkerRpcResponse;
    try {
      response = parseWorkerRpcResponse(message);
    } catch {
      return;
    }
    const entry = this.pending.get(response.id);
    if (!entry) return;
    this.pending.delete(response.id);
    clearTimeout(entry.timer);
    if (response.ok) {
      if (byteLength(response.result) > WORKER_RPC_MAX_RESPONSE_BYTES) {
        entry.reject(new Error("worker_rpc_result_too_large"));
        return;
      }
      entry.resolve(response.result);
      return;
    }
    const code = response.error?.code ?? "worker_rpc_failed";
    entry.reject(new Error(SAFE_ERROR_CODE.test(code) ? code : "worker_rpc_failed"));
  }
}

function rpcErrorCode(error: unknown): string {
  const candidate = error instanceof Error ? error.message : undefined;
  return candidate && SAFE_ERROR_CODE.test(candidate) ? candidate : "worker_rpc_failed";
}

/**
 * Child-side control-plane facade: internal tools and MCP accounting travel
 * as bounded IPC RPCs; the parent owns the HMAC secret and the Run lease.
 */
export class WorkerRpcControlPlane implements McpAccountingControlPlane, ControlPlaneTransport {
  constructor(private readonly rpc: WorkerRpcClient) {}

  async executeInternalTool(toolName: string, args: Record<string, unknown>): Promise<unknown> {
    return this.rpc.call("internal_tool", { tool_name: toolName, args });
  }

  async preflight(input: McpToolCallInput): Promise<McpPermit | McpBlocked> {
    try {
      const result = await this.rpc.call("mcp_preflight", {
        tool: input.tool,
        server: input.server,
        args: input.args,
      });
      if (!isRecord(result) || typeof result.permit_id !== "string" || result.permit_id.length === 0) {
        return { block: true, reason: "mcp_permit_invalid" };
      }
      return result as McpPermit;
    } catch (error) {
      return { block: true, reason: rpcErrorCode(error) };
    }
  }

  async finalize(permit: McpPermit, result: unknown): Promise<unknown> {
    return this.rpc.call("mcp_finalize", { permit_id: permit.permit_id, details: result });
  }

  async fail(
    permit: McpPermit,
    classification: "definitely_not_sent" | "failed_confirmed" | "result_unknown",
  ): Promise<unknown> {
    return this.rpc.call("mcp_fail", { permit_id: permit.permit_id, classification });
  }
}
