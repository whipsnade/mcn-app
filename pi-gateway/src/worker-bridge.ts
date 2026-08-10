/**
 * Parent-side half of the worker IPC RPC bridge.
 *
 * The parent holds the control-plane client (HMAC secret) and the Run lease
 * token; the isolated child only sees bounded, validated RPC requests.  Every
 * handler re-validates its params before touching FastAPI.
 */

import type { ChildProcess } from "node:child_process";

import type { ControlPlaneClient } from "./control-plane-client.js";
import type { McpToolCallInput } from "./mcp-accounting-extension.js";
import {
  parseWorkerRpcRequest,
  WORKER_RPC_MAX_RESPONSE_BYTES,
  type WorkerRpcMethod,
  type WorkerRpcResponse,
} from "./ipc-rpc.js";

export type WorkerRpcHandler = (params: Record<string, unknown>) => Promise<unknown>;

export type WorkerRpcHandlers = Record<WorkerRpcMethod, WorkerRpcHandler>;

const SAFE_ERROR_CODE = /^[a-z0-9_:-]{1,64}$/;
const MCP_CLASSIFICATIONS = new Set(["definitely_not_sent", "failed_confirmed", "result_unknown"]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function requireString(value: unknown, max: number): string {
  if (typeof value !== "string" || value.length === 0 || value.length > max) {
    throw new Error("worker_rpc_params_invalid");
  }
  return value;
}

function requireRecord(value: unknown): Record<string, unknown> {
  if (!isRecord(value)) throw new Error("worker_rpc_params_invalid");
  if (Object.keys(value).length > 64) throw new Error("worker_rpc_params_invalid");
  return value;
}

function safeErrorCode(error: unknown): string {
  const candidate =
    error && typeof error === "object" && "code" in error
      ? (error as { code?: unknown }).code
      : error instanceof Error
        ? error.message
        : undefined;
  return typeof candidate === "string" && SAFE_ERROR_CODE.test(candidate)
    ? candidate
    : "worker_rpc_failed";
}

function byteLength(value: unknown): number {
  return new TextEncoder().encode(JSON.stringify(value)).byteLength;
}

/**
 * Bind bridge handlers to one claimed Run: identity and the lease token come
 * from the authenticated claim, never from the RPC payload.
 */
export function createWorkerRpcHandlers(
  controlPlane: ControlPlaneClient,
  claim: { run_id: string; attempt_id: string; lease_token: string },
): WorkerRpcHandlers {
  return {
    internal_tool: async (params) => {
      const toolName = requireString(params.tool_name, 128);
      const args = requireRecord(params.args ?? {});
      return controlPlane.executeInternalTool(
        toolName,
        args,
        claim.run_id,
        claim.attempt_id,
        claim.lease_token,
      );
    },
    mcp_preflight: async (params) => {
      const input: McpToolCallInput = {
        tool: requireString(params.tool, 128),
        server: requireString(params.server, 64),
        args: requireRecord(params.args ?? {}),
      };
      return controlPlane.preflightMcp(claim.run_id, input, claim.lease_token);
    },
    mcp_finalize: async (params) => {
      const permitId = requireString(params.permit_id, 64);
      const details = requireRecord(params.details ?? {});
      return controlPlane.finalizeMcp(claim.run_id, { permit_id: permitId }, details, claim.lease_token);
    },
    mcp_fail: async (params) => {
      const permitId = requireString(params.permit_id, 64);
      const classification = requireString(params.classification, 64);
      if (!MCP_CLASSIFICATIONS.has(classification)) throw new Error("worker_rpc_params_invalid");
      return controlPlane.failMcp(
        claim.run_id,
        { permit_id: permitId },
        classification as "definitely_not_sent" | "failed_confirmed" | "result_unknown",
        claim.lease_token,
      );
    },
  };
}

/**
 * Attach the RPC dispatch loop to a spawned child.  Returns an idempotent
 * detach; the bridge always detaches itself on child exit so no listener or
 * handler outlives the worker.
 */
export function attachWorkerRpcBridge(
  child: ChildProcess,
  handlers: WorkerRpcHandlers,
  options: { timeoutMs?: number } = {},
): () => void {
  const timeoutMs = options.timeoutMs ?? 60_000;
  const respond = (response: WorkerRpcResponse): void => {
    try {
      if (child.connected) child.send(response);
    } catch {
      // The child is gone; pending child-side calls fail on their own dispose.
    }
  };
  const listener = (message: unknown): void => {
    let requestId: string | undefined;
    try {
      const request = parseWorkerRpcRequest(message);
      requestId = request.id;
      const handler = handlers[request.method];
      void (async () => {
        let timer: ReturnType<typeof setTimeout> | undefined;
        try {
          const result = await Promise.race([
            handler(request.params),
            new Promise<never>((_resolve, reject) => {
              timer = setTimeout(() => reject(new Error("worker_rpc_timeout")), timeoutMs);
            }),
          ]);
          if (byteLength(result) > WORKER_RPC_MAX_RESPONSE_BYTES) {
            respond({ type: "worker_rpc_result", id: request.id, ok: false, error: { code: "worker_rpc_result_too_large" } });
            return;
          }
          // The strict wire contract has no undefined success payload.
          respond({ type: "worker_rpc_result", id: request.id, ok: true, result: result ?? {} });
        } catch (error) {
          respond({
            type: "worker_rpc_result",
            id: request.id,
            ok: false,
            error: { code: safeErrorCode(error) },
          });
        } finally {
          if (timer !== undefined) clearTimeout(timer);
        }
      })();
    } catch {
      if (requestId) {
        respond({ type: "worker_rpc_result", id: requestId, ok: false, error: { code: "worker_rpc_invalid" } });
        return;
      }
      if (isRecord(message) && typeof message.id === "string") {
        respond({
          type: "worker_rpc_result",
          id: message.id,
          ok: false,
          error: { code: "worker_rpc_invalid" },
        });
      }
    }
  };
  let detached = false;
  const detach = (): void => {
    if (detached) return;
    detached = true;
    child.removeListener("message", listener);
    child.removeListener("exit", detach);
  };
  child.on("message", listener);
  child.once("exit", detach);
  return detach;
}
