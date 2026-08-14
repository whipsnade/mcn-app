import { describe, expect, it, vi } from "vitest";

import {
  buildMcpFinalizeMetadata,
  classifyMcpFailure,
  createMcpAccountingExtensionFactory,
  isSafeMcpFinalizeMetadata,
  McpAccountingExtension,
  type McpFinalizeMetadata,
} from "../src/mcp-accounting-extension.js";

function hooks() {
  const handlers = new Map<string, (event: any) => Promise<unknown>>();
  const install = (extension: McpAccountingExtension, bindings = [
    { toolName: "query_analysis_data", server: "insight-cube-mcp", remoteName: "query" },
  ]) => {
    createMcpAccountingExtensionFactory(extension, bindings)({
      on: (name: string, handler: (event: any) => Promise<unknown>) => handlers.set(name, handler),
    } as any);
  };
  return { handlers, install };
}

describe("direct MCP accounting hook", () => {
  it("copies only approved control metadata and never parses business result", () => {
    const metadata = buildMcpFinalizeMetadata({
      upstream_request_id: "upstream-1",
      response_bytes: 42,
      adapter_version: "adapter-v1",
      completed_at: "2026-08-13T10:00:00Z",
      response_hash: `sha256:${"a".repeat(64)}`,
      structuredContent: { rows: [{ secret: "business" }] },
      result: '{"rows":[1]}',
    });
    expect(metadata).toEqual({
      outcome: "succeeded",
      upstream_request_id: "upstream-1",
      response_bytes: 42,
      adapter_version: "adapter-v1",
      completed_at: "2026-08-13T10:00:00Z",
      response_hash: `sha256:${"a".repeat(64)}`,
    });
    expect(JSON.stringify(metadata)).not.toContain("business");
    expect(isSafeMcpFinalizeMetadata(metadata)).toBe(true);
    expect(isSafeMcpFinalizeMetadata({
      outcome: "succeeded",
      details: { structuredContent: { rows: [1] } },
    })).toBe(false);
  });

  it.each([
    [{ structuredContent: { rows: [{ id: "row-1" }] } }, [{ type: "text", text: "adapter text" }]],
    [{}, [{ type: "text", text: "plain text" }]],
    [{}, [{ type: "text", text: "first" }, { type: "text", text: "second" }]],
    [{}, [{ type: "text", text: '{"result":"{\\"rows\\":[]}"}' }]],
    [{}, []],
  ])("leaves every standard Tool Result unchanged and settles sideband", async (details, content) => {
    const finalize = vi.fn(async (_permit: unknown, _metadata: McpFinalizeMetadata) => ({ ok: true }));
    const extension = new McpAccountingExtension({
      preflight: vi.fn(async () => ({ permit_id: "permit-1" })),
      finalize,
      fail: vi.fn(),
    });
    const { handlers, install } = hooks();
    install(extension);
    await handlers.get("tool_call")?.({
      type: "tool_call", toolName: "query_analysis_data", toolCallId: "tool-1", input: { keyword: "咖啡" },
    });
    const event = {
      type: "tool_result", toolName: "query_analysis_data", toolCallId: "tool-1",
      input: { keyword: "咖啡" }, content, details, isError: false,
    };
    const original = structuredClone(content);
    await expect(handlers.get("tool_result")?.(event)).resolves.toBeUndefined();
    expect(event.content).toEqual(original);
    expect(finalize).toHaveBeenCalledWith(
      { permit_id: "permit-1" },
      expect.objectContaining({ outcome: "succeeded" }),
    );
    const finalizeMetadata = (finalize.mock.calls[0] as unknown[] | undefined)?.[1];
    expect(JSON.stringify(finalizeMetadata)).not.toContain("rows");
  });

  it("keeps Scenario 1 bare MCP names mapped to the reviewed server", async () => {
    const preflight = vi.fn(async () => ({ permit_id: "permit-1" }));
    const extension = new McpAccountingExtension({ preflight, finalize: vi.fn(), fail: vi.fn() });
    const { handlers, install } = hooks();
    install(extension, [{ toolName: "query_analysis_data", server: "insight-cube-mcp", remoteName: "query" }]);
    const input = { tool: "query", args: { keyword: "美妆" } };
    await handlers.get("tool_call")?.({ type: "tool_call", toolName: "mcp", toolCallId: "tool-1", input });
    expect(input).toMatchObject({ server: "insight-cube-mcp" });
    expect(preflight).toHaveBeenCalledWith({
      tool: "query_analysis_data", server: "insight-cube-mcp", args: { keyword: "美妆" },
    });
  });

  it.each([
    ["tool_error", "failed_confirmed"],
    ["call_failed", "result_unknown"],
    ["server_not_connected", "definitely_not_sent"],
  ] as const)("preserves adapter failure classification (%s)", async (code, classification) => {
    const fail = vi.fn(async () => ({ ok: true }));
    const extension = new McpAccountingExtension({
      preflight: vi.fn(async () => ({ permit_id: "permit-1" })), finalize: vi.fn(), fail,
    });
    const { handlers, install } = hooks();
    install(extension);
    await handlers.get("tool_call")?.({
      type: "tool_call", toolName: "query_analysis_data", toolCallId: "tool-1", input: {},
    });
    await handlers.get("tool_result")?.({
      type: "tool_result", toolName: "query_analysis_data", toolCallId: "tool-1",
      content: [{ type: "text", text: "adapter error" }], isError: true, details: { error: code },
    });
    expect(fail).toHaveBeenCalledWith(
      { permit_id: "permit-1" },
      classification,
      ...(classification === "result_unknown"
        ? [expect.objectContaining({
            version: "mcp_failure_v1",
            source: code === "call_failed" ? "call_failed" : "other",
          })]
        : []),
    );
    // 提交 3：metadata-only 可观测性——call_failed → result_unknown 且携带
    // error_class（adapter error code）与 dispatch_phase=dispatched。
    if (classification === "result_unknown") {
      const metadata = (fail.mock.calls[0] as unknown[] | undefined)?.[2];
      expect(metadata).toMatchObject({
        version: "mcp_failure_v1",
        source: code === "call_failed" ? "call_failed" : "other",
        error_class: code,
      });
      if (code === "call_failed") {
        expect(metadata).toMatchObject({ dispatch_phase: "dispatched" });
      }
    }
  });

  it.each([
    [true, "some_future_error"],
    [false, "some_future_error"],
  ])("keeps an unknown adapter error code as result_unknown (isError=%s)", async (isError, code) => {
    const fail = vi.fn(async () => ({ ok: true }));
    const extension = new McpAccountingExtension({
      preflight: vi.fn(async () => ({ permit_id: "permit-1" })), finalize: vi.fn(), fail,
    });
    const { handlers, install } = hooks();
    install(extension);
    await handlers.get("tool_call")?.({
      type: "tool_call", toolName: "query_analysis_data", toolCallId: "tool-1", input: {},
    });
    await handlers.get("tool_result")?.({
      type: "tool_result", toolName: "query_analysis_data", toolCallId: "tool-1",
      content: [{ type: "text", text: "adapter error" }], isError, details: { error: code },
    });
    expect(fail).toHaveBeenCalledWith(
      { permit_id: "permit-1" },
      "result_unknown",
      expect.objectContaining({ version: "mcp_failure_v1", source: "other" }),
    );
    // 未知 error code 无法确认外发阶段：dispatch_phase=unknown 且保留 error_class。
    const metadata = (fail.mock.calls[0] as unknown[] | undefined)?.[2];
    expect(metadata).toMatchObject({
      version: "mcp_failure_v1",
      source: "other",
      error_class: code,
      dispatch_phase: "unknown",
      ...(isError
        ? { is_standard_mcp_error: true, received_jsonrpc_response: true }
        : {}),
    });
    if (isError) {
      // Minor #2：SDK isError 标记确认收到标准 MCP error 响应 → 赋值 true；
      // 未确认路径保持省略（不猜 false）。
      expect(metadata).not.toHaveProperty("received_jsonrpc_response", false);
    }
  });

  it("treats the SDK isError marker without an error code as a confirmed Tool Error", async () => {
    const fail = vi.fn(async () => ({ ok: true }));
    const extension = new McpAccountingExtension({
      preflight: vi.fn(async () => ({ permit_id: "permit-1" })), finalize: vi.fn(), fail,
    });
    const { handlers, install } = hooks();
    install(extension);
    await handlers.get("tool_call")?.({
      type: "tool_call", toolName: "query_analysis_data", toolCallId: "tool-1", input: {},
    });
    await handlers.get("tool_result")?.({
      type: "tool_result", toolName: "query_analysis_data", toolCallId: "tool-1",
      content: [{ type: "text", text: "standard MCP error" }], isError: true, details: {},
    });

    expect(fail).toHaveBeenCalledWith({ permit_id: "permit-1" }, "failed_confirmed");
  });

  it("keeps the permit recoverable when the accounting ACK is not confirmed", async () => {
    const fail = vi.fn(async () => { throw new Error("worker_rpc_timeout"); });
    const extension = new McpAccountingExtension({
      preflight: vi.fn(async () => ({ permit_id: "permit-1" })),
      finalize: vi.fn(async () => { throw new Error("worker_rpc_timeout"); }),
      fail,
    });
    const { handlers, install } = hooks();
    install(extension);
    await handlers.get("tool_call")?.({
      type: "tool_call", toolName: "query_analysis_data", toolCallId: "tool-1", input: {},
    });
    await handlers.get("tool_result")?.({
      type: "tool_result", toolName: "query_analysis_data", toolCallId: "tool-1",
      content: [{ type: "text", text: "success" }], isError: false, details: {},
    });
    expect(fail).toHaveBeenCalledWith(
      { permit_id: "permit-1" },
      "result_unknown",
      expect.objectContaining({ version: "mcp_failure_v1", source: "worker_rpc_timeout" }),
    );
  });
});

describe("MCP failure classifier", () => {
  it("does not infer business outcome from content", () => {
    expect(classifyMcpFailure(undefined, { classification: "result_unknown" })).toBe("result_unknown");
    expect(classifyMcpFailure("tool_error", { content: "success" })).toBe("failed_confirmed");
  });
});
