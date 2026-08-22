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

  it("classifies call_failed with a confirmed standard MCP error as failed_confirmed", async () => {
    const fail = vi.fn(async () => ({ ok: true }));
    const extension = new McpAccountingExtension({
      preflight: vi.fn(async () => ({ permit_id: "permit-1" })), finalize: vi.fn(), fail,
    });
    const { handlers, install } = hooks();
    install(extension);
    await handlers.get("tool_call")?.({
      type: "tool_call", toolName: "query_analysis_data", toolCallId: "tool-1", input: {},
    });
    const content = [{ type: "text", text: "standard MCP error" }];
    const original = structuredClone(content);
    const event = {
      type: "tool_result", toolName: "query_analysis_data", toolCallId: "tool-1",
      content, isError: true, details: { error: "call_failed" },
    };
    await handlers.get("tool_result")?.(event);
    expect(event.content).toEqual(original);
    expect(fail).toHaveBeenCalledTimes(1);
    expect(fail).toHaveBeenCalledWith(
      { permit_id: "permit-1" },
      "failed_confirmed",
      {
        version: "mcp_failure_v1",
        source: "call_failed",
        error_class: "call_failed",
        dispatch_phase: "dispatched",
        is_standard_mcp_error: true,
        received_jsonrpc_response: true,
      },
    );
  });

  it.each([
    ["tool_error", true, "failed_confirmed"],
    ["call_failed", true, "failed_confirmed"],
    ["call_failed", false, "result_unknown"],
    ["server_not_connected", true, "definitely_not_sent"],
  ] as const)("preserves adapter failure classification (%s, isError=%s)", async (code, isError, classification) => {
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
    if (classification === "definitely_not_sent") {
      expect(fail).toHaveBeenCalledWith({ permit_id: "permit-1" }, "definitely_not_sent");
      return;
    }
    expect(fail).toHaveBeenCalledWith(
      { permit_id: "permit-1" },
      classification,
      expect.objectContaining({
        version: "mcp_failure_v1",
        source: code === "call_failed" ? "call_failed" : "other",
        error_class: code,
        ...(isError ? { is_standard_mcp_error: true, received_jsonrpc_response: true } : {}),
      }),
    );
    const metadata = (fail.mock.calls[0] as unknown[] | undefined)?.[2];
    if (code === "call_failed") {
      expect(metadata).toMatchObject({ dispatch_phase: "dispatched" });
    }
  });

  it.each([
    [true, "some_future_error", "failed_confirmed"],
    [false, "some_future_error", "result_unknown"],
  ])("classifies an unknown adapter error code by the response signal (isError=%s)", async (isError, code, classification) => {
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
      classification,
      expect.objectContaining({ version: "mcp_failure_v1", source: "other" }),
    );
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

    expect(fail).toHaveBeenCalledWith(
      { permit_id: "permit-1" },
      "failed_confirmed",
      expect.objectContaining({
        version: "mcp_failure_v1",
        is_standard_mcp_error: true,
        received_jsonrpc_response: true,
      }),
    );
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
