import { describe, expect, it, vi } from "vitest";

import {
  createMcpAccountingExtensionFactory,
  McpAccountingExtension,
} from "../src/mcp-accounting-extension.js";

describe("McpAccountingExtension", () => {
  it("commits a permit before calling the adapter and finalizes after result", async () => {
    const calls: string[] = [];
    const control = {
      preflight: vi.fn(async () => { calls.push("preflight"); return { permit_id: "p-1" }; }),
      finalize: vi.fn(async () => { calls.push("finalize"); return { ok: true }; }),
      fail: vi.fn(),
    };
    const extension = new McpAccountingExtension(control);
    const permit = await extension.beforeToolCall({ tool: "query_analysis_data", server: "insight", args: { q: "x" } });
    expect("permit_id" in permit).toBe(true);
    calls.push("mcp");
    await extension.afterToolResult(permit as { permit_id: string }, { mode: "mcpResult", value: { ok: true } });
    expect(calls).toEqual(["preflight", "mcp", "finalize"]);
  });

  it("does not bill connect/search/list discovery calls", async () => {
    const control = { preflight: vi.fn(), finalize: vi.fn(), fail: vi.fn() };
    const extension = new McpAccountingExtension(control);
    for (const tool of ["connect", "search", "list"]) {
      const result = await extension.beforeToolCall({ tool, server: "insight", args: {} });
      expect(result).toEqual({ free: true });
    }
    expect(control.preflight).not.toHaveBeenCalled();
  });

  it("registers the permit boundary on the SDK tool_call/tool_result hooks", async () => {
    const calls: string[] = [];
    const handlers = new Map<string, (event: any) => Promise<unknown>>();
    const control = {
      preflight: vi.fn(async () => { calls.push("preflight"); return { permit_id: "p-1" }; }),
      finalize: vi.fn(async () => { calls.push("finalize"); return { ok: true }; }),
      fail: vi.fn(),
    };
    const extension = new McpAccountingExtension(control);
    createMcpAccountingExtensionFactory(extension, [
      { toolName: "insight_query", server: "insight", remoteName: "query" },
    ])({
      on: (name: string, handler: (event: any) => Promise<unknown>) => handlers.set(name, handler),
    } as any);

    const before = await handlers.get("tool_call")?.({
      type: "tool_call", toolName: "insight_query", toolCallId: "tc-1", input: { q: "x" },
    });
    expect(before).toBeUndefined();
    expect(control.preflight).toHaveBeenCalledWith({
      tool: "insight_query",
      server: "insight",
      args: { q: "x" },
    });
    calls.push("mcp");
    await handlers.get("tool_result")?.({
      type: "tool_result", toolName: "insight_query", toolCallId: "tc-1", input: { q: "x" },
      content: [{ type: "text", text: "ok" }], isError: false, details: { mode: "mcpResult" },
    });
    expect(calls).toEqual(["preflight", "mcp", "finalize"]);
  });

  it("returns an SDK block result when preflight is unavailable", async () => {
    const handlers = new Map<string, (event: any) => Promise<unknown>>();
    const extension = new McpAccountingExtension({
      preflight: vi.fn(async () => { throw new Error("network"); }),
      finalize: vi.fn(),
      fail: vi.fn(),
    });
    createMcpAccountingExtensionFactory(extension)({
      on: (name: string, handler: (event: any) => Promise<unknown>) => handlers.set(name, handler),
    } as any);
    await expect(handlers.get("tool_call")?.({
      type: "tool_call", toolName: "mcp", toolCallId: "tc-1",
      input: { tool: "query", server: "insight", args: {} },
    })).resolves.toEqual({ block: true, reason: "control_plane_unreachable" });
  });

  it("maps adapter proxy names back to reviewed catalog identities", async () => {
    const handlers = new Map<string, (event: any) => Promise<unknown>>();
    const control = {
      preflight: vi.fn(async () => ({ permit_id: "p-1" })),
      finalize: vi.fn(),
      fail: vi.fn(),
    };
    const extension = new McpAccountingExtension(control);
    createMcpAccountingExtensionFactory(extension, [
      {
        toolName: "query_analysis_data",
        server: "insight-cube",
        remoteName: "datatap.insight.query.analysis.v1",
      },
    ])({
      on: (name: string, handler: (event: any) => Promise<unknown>) => handlers.set(name, handler),
    } as any);

    await handlers.get("tool_call")?.({
      type: "tool_call",
      toolName: "mcp",
      toolCallId: "tc-9",
      input: {
        tool: "insight_cube_datatap_insight_query_analysis_v1",
        server: "insight-cube",
        args: { keyword: "美妆" },
      },
    });

    expect(control.preflight).toHaveBeenCalledWith({
      tool: "query_analysis_data",
      server: "insight-cube",
      args: { keyword: "美妆" },
    });
  });

  // 本地未外发错误必须 definitely_not_sent（释放预留）；带 details.error 的
  // 结果绝不允许进入成功结算分支——无论 isError 是否为 true。
  it.each([
    "server_backoff",
    "connect_failed",
    "not_connected",
    "server_not_connected",
    "auth_required",
    "not_authenticated",
    "not_initialized",
    "init_failed",
    "init_timeout",
    "server_disabled",
    "server_unavailable",
    "missing_server",
    "tool_not_found",
    "tool_not_found_after_reconnect",
  ])("classifies local no-dispatch error %s as definitely_not_sent", async (code) => {
    const handlers = new Map<string, (event: any) => Promise<unknown>>();
    const control = {
      preflight: vi.fn(async () => ({ permit_id: "p-1" })),
      finalize: vi.fn(),
      fail: vi.fn(async () => ({ ok: true })),
    };
    const extension = new McpAccountingExtension(control);
    createMcpAccountingExtensionFactory(extension, [
      { toolName: "insight_query", server: "insight", remoteName: "query" },
    ])({
      on: (name: string, handler: (event: any) => Promise<unknown>) => handlers.set(name, handler),
    } as any);

    await handlers.get("tool_call")?.({
      type: "tool_call", toolName: "insight_query", toolCallId: "tc-1", input: {},
    });
    await handlers.get("tool_result")?.({
      type: "tool_result", toolName: "insight_query", toolCallId: "tc-1",
      content: [{ type: "text", text: "server not connected" }],
      isError: false,
      details: { error: code },
    });

    expect(control.fail).toHaveBeenCalledWith({ permit_id: "p-1" }, "definitely_not_sent");
    expect(control.finalize).not.toHaveBeenCalled();
  });

  it("classifies a server-confirmed error result as failed_confirmed", async () => {
    const handlers = new Map<string, (event: any) => Promise<unknown>>();
    const control = {
      preflight: vi.fn(async () => ({ permit_id: "p-1" })),
      finalize: vi.fn(),
      fail: vi.fn(async () => ({ ok: true })),
    };
    const extension = new McpAccountingExtension(control);
    createMcpAccountingExtensionFactory(extension, [
      { toolName: "insight_query", server: "insight", remoteName: "query" },
    ])({
      on: (name: string, handler: (event: any) => Promise<unknown>) => handlers.set(name, handler),
    } as any);

    await handlers.get("tool_call")?.({
      type: "tool_call", toolName: "insight_query", toolCallId: "tc-1", input: {},
    });
    await handlers.get("tool_result")?.({
      type: "tool_result", toolName: "insight_query", toolCallId: "tc-1",
      content: [{ type: "text", text: "remote validation failed" }],
      isError: true,
      details: { error: "tool_error" },
    });

    expect(control.fail).toHaveBeenCalledWith({ permit_id: "p-1" }, "failed_confirmed");
    expect(control.finalize).not.toHaveBeenCalled();
  });

  it("keeps thrown mid-flight calls and unknown error codes as result_unknown", async () => {
    const handlers = new Map<string, (event: any) => Promise<unknown>>();
    const control = {
      preflight: vi.fn(async () => ({ permit_id: "p-1" })),
      finalize: vi.fn(),
      fail: vi.fn(async () => ({ ok: true })),
    };
    const extension = new McpAccountingExtension(control);
    createMcpAccountingExtensionFactory(extension, [
      { toolName: "insight_query", server: "insight", remoteName: "query" },
    ])({
      on: (name: string, handler: (event: any) => Promise<unknown>) => handlers.set(name, handler),
    } as any);

    for (const [index, code] of ["call_failed", "some_future_error"].entries()) {
      await handlers.get("tool_call")?.({
        type: "tool_call", toolName: "insight_query", toolCallId: `tc-${index}`, input: {},
      });
      await handlers.get("tool_result")?.({
        type: "tool_result", toolName: "insight_query", toolCallId: `tc-${index}`,
        content: [], isError: true, details: { error: code },
      });
    }

    expect(control.fail).toHaveBeenNthCalledWith(1, { permit_id: "p-1" }, "result_unknown");
    expect(control.fail).toHaveBeenNthCalledWith(2, { permit_id: "p-1" }, "result_unknown");
    expect(control.finalize).not.toHaveBeenCalled();
  });
});
