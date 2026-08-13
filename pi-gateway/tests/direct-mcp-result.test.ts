import { describe, expect, it, vi } from "vitest";

import {
  createMcpAccountingExtensionFactory,
  McpAccountingExtension,
  type McpFinalizeMetadata,
} from "../src/mcp-accounting-extension.js";
import {
  parseWorkerRpcRequest,
  WorkerRpcControlPlane,
  WorkerRpcClient,
} from "../src/ipc-rpc.js";

function memoryChannel() {
  const listeners = new Set<(message: unknown) => void>();
  const sent: unknown[] = [];
  return {
    sent,
    send(message: unknown) { sent.push(message); },
    onMessage(listener: (message: unknown) => void) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    emit(message: unknown) { for (const listener of listeners) listener(message); },
  };
}

describe("direct MCP result architecture", () => {
  it.each([
    [{ structuredContent: { rows: [{ id: "row-1" }] } }, [{ type: "text", text: "adapter text" }]],
    [{ structuredContent: undefined }, [{ type: "text", text: "plain text" }]],
    [{}, [{ type: "text", text: "first" }, { type: "text", text: "second" }]],
    [{}, [{ type: "text", text: '{"result":"{\\"rows\\":[]}"}' }]],
  ])("passes the standard Tool Result through and only side-observes safe metadata", async (details, content) => {
    const handlers = new Map<string, (event: any) => Promise<unknown>>();
    const finalize = vi.fn(async (_permit: unknown, _metadata: McpFinalizeMetadata) => ({ ok: true }));
    const accounting = new McpAccountingExtension({
      preflight: vi.fn(async () => ({ permit_id: "permit-1" })),
      finalize,
      fail: vi.fn(),
    });
    createMcpAccountingExtensionFactory(accounting, [
      { toolName: "query_analysis_data", server: "insight-cube-mcp", remoteName: "query" },
    ])({ on: (name: string, handler: (event: any) => Promise<unknown>) => handlers.set(name, handler) } as any);

    await handlers.get("tool_call")?.({
      type: "tool_call", toolName: "query_analysis_data", toolCallId: "tool-1", input: { keyword: "咖啡" },
    });
    const event = {
      type: "tool_result", toolName: "query_analysis_data", toolCallId: "tool-1", input: { keyword: "咖啡" },
      content, details, isError: false,
    };
    const originalContent = structuredClone(content);
    await expect(handlers.get("tool_result")?.(event)).resolves.toBeUndefined();

    expect(event.content).toEqual(originalContent);
    expect(finalize).toHaveBeenCalledTimes(1);
    const metadata = finalize.mock.calls[0]?.[1] as unknown as Record<string, unknown>;
    expect(metadata).toMatchObject({ outcome: "succeeded" });
    expect(metadata).not.toHaveProperty("structuredContent");
    expect(metadata).not.toHaveProperty("content");
    expect(JSON.stringify(metadata)).not.toContain("rows");
  });

  it("uses a small metadata-only finalize RPC and rejects the old details envelope", async () => {
    const channel = memoryChannel();
    const client = new WorkerRpcClient(channel);
    const controlPlane = new WorkerRpcControlPlane(client);
    const pending = controlPlane.finalize(
      { permit_id: "permit-1" },
      { outcome: "succeeded", upstream_request_id: "upstream-1", response_bytes: 42 },
    );
    const request = channel.sent[0] as { id: string; method: string; params: Record<string, unknown> };
    expect(request).toMatchObject({
      method: "mcp_finalize",
      params: { permit_id: "permit-1", outcome: "succeeded", upstream_request_id: "upstream-1", response_bytes: 42 },
    });
    expect(request.params).not.toHaveProperty("details");
    expect(() => parseWorkerRpcRequest({
      type: "worker_rpc", id: "rpc-1", method: "mcp_finalize",
      params: { permit_id: "permit-1", details: { structuredContent: { secret: "business" } } },
    })).toThrow("worker_rpc_invalid");
    channel.emit({ type: "worker_rpc_result", id: request.id, ok: true, result: {} });
    await expect(pending).resolves.toEqual({});
    client.dispose();
  });
});
