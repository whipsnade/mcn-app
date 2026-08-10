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
});
