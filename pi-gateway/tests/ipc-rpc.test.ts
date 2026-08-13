import { describe, expect, it } from "vitest";

import {
  parseWorkerRpcRequest,
  parseWorkerRpcResponse,
  WorkerRpcClient,
  WorkerRpcControlPlane,
  WORKER_RPC_MAX_REQUEST_BYTES,
  type WorkerRpcChannel,
} from "../src/ipc-rpc.js";

function memoryChannel(): WorkerRpcChannel & {
  sent: unknown[];
  emit(message: unknown): void;
  emitDisconnect(): void;
} {
  const listeners = new Set<(message: unknown) => void>();
  const disconnectListeners = new Set<() => void>();
  return {
    sent: [],
    emit(message: unknown) {
      for (const listener of listeners) listener(message);
    },
    emitDisconnect() {
      for (const listener of disconnectListeners) listener();
    },
    send(message: unknown) {
      (this as { sent: unknown[] }).sent.push(message);
    },
    onMessage(listener: (message: unknown) => void) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    onDisconnect(listener: () => void) {
      disconnectListeners.add(listener);
      return () => disconnectListeners.delete(listener);
    },
  };
}

describe("worker IPC RPC protocol", () => {
  it("accepts only typed, bounded, allowlisted requests", () => {
    const request = parseWorkerRpcRequest({
      type: "worker_rpc",
      id: "call-1",
      method: "internal_tool",
      params: { tool_name: "get_session_context", args: {} },
    });
    expect(request.method).toBe("internal_tool");

    for (const value of [
      null,
      {},
      { type: "worker_rpc", id: "", method: "internal_tool", params: {} },
      { type: "worker_rpc", id: "x", method: "shell", params: {} },
      { type: "worker_rpc", id: "x", method: "internal_tool", params: [] },
      { type: "worker_rpc", id: "x", method: "internal_tool", params: {}, extra: 1 },
      { type: "event", id: "x", method: "internal_tool", params: {} },
    ]) {
      expect(() => parseWorkerRpcRequest(value)).toThrow("worker_rpc_invalid");
    }
    const oversized = "x".repeat(WORKER_RPC_MAX_REQUEST_BYTES);
    expect(() =>
      parseWorkerRpcRequest({
        type: "worker_rpc",
        id: "call-1",
        method: "internal_tool",
        params: { tool_name: "get_session_context", args: { blob: oversized } },
      }),
    ).toThrow("worker_rpc_invalid");
  });

  it("accepts only correlated typed responses", () => {
    expect(
      parseWorkerRpcResponse({ type: "worker_rpc_result", id: "a", ok: true, result: { x: 1 } }),
    ).toMatchObject({ id: "a", ok: true });
    for (const value of [
      null,
      { type: "worker_rpc_result" },
      { type: "worker_rpc_result", id: "a" },
      { type: "worker_rpc_result", id: "a", ok: true, result: undefined },
      { type: "worker_rpc_result", id: "a", ok: false },
      { type: "worker_rpc_result", id: "a", ok: false, error: {} },
      { type: "worker_rpc_result", id: "a", ok: true, result: {}, extra: 1 },
    ]) {
      expect(() => parseWorkerRpcResponse(value)).toThrow();
    }
  });

  it("correlates concurrent calls and resolves out of order", async () => {
    const channel = memoryChannel();
    const client = new WorkerRpcClient(channel);
    const first = client.call("internal_tool", { tool_name: "read_artifact", args: {} });
    const second = client.call("mcp_preflight", { tool: "t", server: "s", args: {} });
    const [firstRequest, secondRequest] = channel.sent as Array<{ id: string }>;
    expect(firstRequest.id).not.toBe(secondRequest.id);
    channel.emit({ type: "worker_rpc_result", id: secondRequest.id, ok: true, result: { permit_id: "p-2" } });
    channel.emit({ type: "worker_rpc_result", id: firstRequest.id, ok: true, result: { text: "artifact" } });
    await expect(second).resolves.toEqual({ permit_id: "p-2" });
    await expect(first).resolves.toEqual({ text: "artifact" });
    expect(client.pendingCount).toBe(0);
    client.dispose();
  });

  it("times out a pending call and ignores a late response", async () => {
    const channel = memoryChannel();
    const client = new WorkerRpcClient(channel, { timeoutMs: 5 });
    const pending = client.call("internal_tool", { tool_name: "read_artifact", args: {} });
    await expect(pending).rejects.toThrow("worker_rpc_timeout");
    expect(client.pendingCount).toBe(0);
    const [request] = channel.sent as Array<{ id: string }>;
    channel.emit({ type: "worker_rpc_result", id: request.id, ok: true, result: {} });
    expect(client.pendingCount).toBe(0);
    client.dispose();
  });

  it("rejects pending calls on dispose and disconnect", async () => {
    const channel = memoryChannel();
    const client = new WorkerRpcClient(channel);
    const pending = client.call("internal_tool", { tool_name: "read_artifact", args: {} });
    channel.emitDisconnect();
    await expect(pending).rejects.toThrow("worker_rpc_disposed");
    await expect(client.call("internal_tool", { tool_name: "x", args: {} })).rejects.toThrow(
      "worker_rpc_disposed",
    );
  });

  it("maps parent error codes and sanitizes unsafe ones", async () => {
    const channel = memoryChannel();
    const client = new WorkerRpcClient(channel);
    const first = client.call("mcp_preflight", { tool: "t", server: "s", args: {} });
    const [request] = channel.sent as Array<{ id: string }>;
    channel.emit({
      type: "worker_rpc_result",
      id: request.id,
      ok: false,
      error: { code: "feature_disabled" },
    });
    await expect(first).rejects.toThrow("feature_disabled");

    const second = client.call("mcp_preflight", { tool: "t", server: "s", args: {} });
    const [, secondRequest] = channel.sent as Array<{ id: string }>;
    channel.emit({
      type: "worker_rpc_result",
      id: secondRequest.id,
      ok: false,
      error: { code: "evil secret leak sk-12345" },
    });
    await expect(second).rejects.toThrow("worker_rpc_failed");
    client.dispose();
  });

  it("bridges MCP accounting with block-on-error semantics", async () => {
    const channel = memoryChannel();
    const client = new WorkerRpcClient(channel);
    const controlPlane = new WorkerRpcControlPlane(client);

    const permitPromise = controlPlane.preflight({ tool: "query", server: "insight-cube", args: {} });
    const [request] = channel.sent as Array<{ id: string; method: string; params: Record<string, unknown> }>;
    expect(request.method).toBe("mcp_preflight");
    expect(request.params).toEqual({ tool: "query", server: "insight-cube", args: {} });
    channel.emit({ type: "worker_rpc_result", id: request.id, ok: true, result: { permit_id: "p-1" } });
    await expect(permitPromise).resolves.toEqual({ permit_id: "p-1" });

    const blocked = controlPlane.preflight({ tool: "query", server: "insight-cube", args: {} });
    const [, blockedRequest] = channel.sent as Array<{ id: string }>;
    channel.emit({
      type: "worker_rpc_result",
      id: blockedRequest.id,
      ok: false,
      error: { code: "insufficient_points" },
    });
    await expect(blocked).resolves.toEqual({ block: true, reason: "insufficient_points" });
    client.dispose();
  });

  it("carries only a small failure source over the MCP fail RPC", async () => {
    const channel = memoryChannel();
    const client = new WorkerRpcClient(channel);
    const controlPlane = new WorkerRpcControlPlane(client);
    const metadata = { version: "mcp_failure_v1" as const, source: "worker_rpc_timeout" as const };
    const pending = controlPlane.fail({ permit_id: "p-unknown" }, "result_unknown", metadata);
    const [request] = channel.sent as Array<{ method: string; params: Record<string, unknown>; id: string }>;
    expect(request).toMatchObject({
      method: "mcp_fail",
      params: { permit_id: "p-unknown", classification: "result_unknown", metadata },
    });
    channel.emit({ type: "worker_rpc_result", id: request.id, ok: true, result: { ok: true } });
    await expect(pending).resolves.toEqual({ ok: true });
    client.dispose();
  });

  it("bridges internal tool execution with tool name and args only", async () => {
    const channel = memoryChannel();
    const client = new WorkerRpcClient(channel);
    const controlPlane = new WorkerRpcControlPlane(client);
    const pending = controlPlane.executeInternalTool("build_brand_report_draft", { scope: {} });
    const [request] = channel.sent as Array<{ method: string; params: Record<string, unknown>; id: string }>;
    expect(request).toMatchObject({
      method: "internal_tool",
      params: { tool_name: "build_brand_report_draft", args: { scope: {} } },
    });
    channel.emit({ type: "worker_rpc_result", id: request.id, ok: true, result: { draft_id: "d-1" } });
    await expect(pending).resolves.toEqual({ draft_id: "d-1" });
    client.dispose();
  });
});
