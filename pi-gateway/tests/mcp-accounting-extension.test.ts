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
    // 身份必须先经 bindings 解析成功，才能走到 preflight 并覆盖
    // control_plane_unreachable 路径（空 bindings 会在本地 identity 闸拦截）。
    createMcpAccountingExtensionFactory(extension, [
      { toolName: "query", server: "insight", remoteName: "query" },
    ])({
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

  it("marks the call result_unknown when the finalize ACK is lost", async () => {
    // finalize 的 durable ACK 未到达：结果不得按成功丢弃，必须降级
    // result_unknown（保留预留、禁止重放）。
    const handlers = new Map<string, (event: any) => Promise<unknown>>();
    const control = {
      preflight: vi.fn(async () => ({ permit_id: "p-1" })),
      finalize: vi.fn(async () => { throw new Error("ack_lost"); }),
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
      content: [{ type: "text", text: "ok" }], isError: false,
      details: { mode: "mcpResult", mcpResult: { structuredContent: { rows: [] } } },
    });

    expect(control.fail).toHaveBeenCalledWith({ permit_id: "p-1" }, "result_unknown");
  });

  it("never settles an oversized result and falls back to result_unknown", async () => {
    const handlers = new Map<string, (event: any) => Promise<unknown>>();
    const control = {
      preflight: vi.fn(async () => ({ permit_id: "p-1" })),
      finalize: vi.fn(async () => ({ ok: true })),
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
      content: [{ type: "text", text: "ok" }], isError: false,
      details: { mode: "mcpResult", mcpResult: { structuredContent: { blob: "x".repeat(2 * 1024 * 1024) } } },
    });

    expect(control.finalize).not.toHaveBeenCalled();
    expect(control.fail).toHaveBeenCalledWith({ permit_id: "p-1" }, "result_unknown");
  });
});

describe("generic mcp proxy identity resolution (real catalog shapes)", () => {
  // 真实 claim adapter_catalog 的 adapter_visible_name = catalog 内部名（实时网关
  // 原样暴露为 remote 名）；bindings 经 protocol.ts 别名映射后 server 为 adapter
  // 别名（insight-cube-mcp → insight-cube）。见 REAL_B7_20260812T045636Z_b801c490
  // L1 失败证据。
  const REAL_BINDINGS = [
    { toolName: "match_best_tag", server: "insight-cube", remoteName: "match_best_tag" },
    { toolName: "kol_detail", server: "social-grow", remoteName: "kol_detail" },
  ];

  function setup(bindings: readonly { toolName: string; server: string; remoteName?: string }[]) {
    const handlers = new Map<string, (event: any) => Promise<unknown>>();
    const control = {
      preflight: vi.fn(async () => ({ permit_id: "p-1" })),
      finalize: vi.fn(),
      fail: vi.fn(),
    };
    const extension = new McpAccountingExtension(control);
    createMcpAccountingExtensionFactory(extension, bindings)({
      on: (name: string, handler: (event: any) => Promise<unknown>) => handlers.set(name, handler),
    } as any);
    return { handlers, control };
  }

  it("resolves a bare remote name with an explicit server to the reviewed internal identity", async () => {
    const { handlers, control } = setup(REAL_BINDINGS);
    const before = await handlers.get("tool_call")?.({
      type: "tool_call", toolName: "mcp", toolCallId: "tc-1",
      input: { tool: "match_best_tag", server: "insight-cube", args: { brand: "瑞幸咖啡" } },
    });
    expect(before).toBeUndefined();
    expect(control.preflight).toHaveBeenCalledTimes(1);
    expect(control.preflight).toHaveBeenCalledWith({
      tool: "match_best_tag", server: "insight-cube", args: { brand: "瑞幸咖啡" },
    });
  });

  it("infers the server when the bare remote name is globally unique", async () => {
    const { handlers, control } = setup(REAL_BINDINGS);
    const input: Record<string, unknown> = { tool: "match_best_tag", args: { brand: "瑞幸咖啡" } };
    const before = await handlers.get("tool_call")?.({
      type: "tool_call", toolName: "mcp", toolCallId: "tc-2",
      input,
    });
    expect(before).toBeUndefined();
    expect(control.preflight).toHaveBeenCalledTimes(1);
    expect(control.preflight).toHaveBeenCalledWith({
      tool: "match_best_tag", server: "insight-cube", args: { brand: "瑞幸咖啡" },
    });
    // 推导出的 server 必须钉回调用入参：adapter 裸名扫描是 first-match，
    // 钉入后分发身份恒等于计费身份（同名 live twin 不会被误分发）。
    expect(input.server).toBe("insight-cube");
  });

  it("fails closed without any preflight when a bare remote name is ambiguous and no server is given", async () => {
    const { handlers, control } = setup([
      { toolName: "get_config", server: "svc-a", remoteName: "get_config" },
      { toolName: "get_config", server: "svc-b", remoteName: "get_config" },
    ]);
    const input: Record<string, unknown> = { tool: "get_config", args: {} };
    const before = await handlers.get("tool_call")?.({
      type: "tool_call", toolName: "mcp", toolCallId: "tc-3",
      input,
    });
    expect(before).toEqual({ block: true, reason: "mcp_tool_identity_ambiguous" });
    expect(control.preflight).not.toHaveBeenCalled();
    expect(input.server).toBeUndefined();  // 失败时不得钉入任何候选
  });

  it("maps an ambiguous remote name exactly when the server is explicit", async () => {
    const { handlers, control } = setup([
      { toolName: "get_config", server: "svc-a", remoteName: "get_config" },
      { toolName: "get_config", server: "svc-b", remoteName: "get_config" },
    ]);
    const before = await handlers.get("tool_call")?.({
      type: "tool_call", toolName: "mcp", toolCallId: "tc-4",
      input: { tool: "get_config", server: "svc-b", args: {} },
    });
    expect(before).toBeUndefined();
    expect(control.preflight).toHaveBeenCalledTimes(1);
    expect(control.preflight).toHaveBeenCalledWith({ tool: "get_config", server: "svc-b", args: {} });
  });

  it("blocks an unknown bare tool name with mcp_tool_identity_invalid and zero preflight", async () => {
    const { handlers, control } = setup(REAL_BINDINGS);
    const before = await handlers.get("tool_call")?.({
      type: "tool_call", toolName: "mcp", toolCallId: "tc-5",
      input: { tool: "no_such_tool", server: "insight-cube", args: {} },
    });
    expect(before).toEqual({ block: true, reason: "mcp_tool_identity_invalid" });
    expect(control.preflight).not.toHaveBeenCalled();
  });

  it("maps the legacy prefixed proxy name to the reviewed identity (dispatch itself fails closed)", async () => {
    const { handlers, control } = setup(REAL_BINDINGS);
    const before = await handlers.get("tool_call")?.({
      type: "tool_call", toolName: "mcp", toolCallId: "tc-6",
      input: { tool: "insight_cube_match_best_tag", args: {} },
    });
    expect(before).toBeUndefined();
    expect(control.preflight).toHaveBeenCalledTimes(1);
    expect(control.preflight).toHaveBeenCalledWith({
      tool: "match_best_tag", server: "insight-cube", args: {},
    });
  });

  it("blocks a slug-prefixed name that no production binding owns", async () => {
    // 历史观测名以 adapter 别名拼前缀（insight_cube_…）；slug 形式
    // （insight_cube_mcp_…）不属于任何 binding——本地拦截，0 preflight。
    const { handlers, control } = setup(REAL_BINDINGS);
    const before = await handlers.get("tool_call")?.({
      type: "tool_call", toolName: "mcp", toolCallId: "tc-6b",
      input: { tool: "insight_cube_mcp_match_best_tag", args: {} },
    });
    expect(before).toEqual({ block: true, reason: "mcp_tool_identity_invalid" });
    expect(control.preflight).not.toHaveBeenCalled();
  });

  it("accepts the dot-sanitized adapter-visible variant of a dotted remote name", async () => {
    const { handlers, control } = setup([
      { toolName: "query_analysis_data", server: "insight-cube", remoteName: "datatap.insight.query.analysis.v1" },
    ]);
    const before = await handlers.get("tool_call")?.({
      type: "tool_call", toolName: "mcp", toolCallId: "tc-6c",
      input: { tool: "datatap_insight_query_analysis_v1", args: {} },
    });
    expect(before).toBeUndefined();
    expect(control.preflight).toHaveBeenCalledWith({
      tool: "query_analysis_data", server: "insight-cube", args: {},
    });
  });

  it("does not regress the direct adapter tool path", async () => {
    const { handlers, control } = setup(REAL_BINDINGS);
    const before = await handlers.get("tool_call")?.({
      type: "tool_call", toolName: "match_best_tag", toolCallId: "tc-7",
      input: { brand: "瑞幸咖啡" },
    });
    expect(before).toBeUndefined();
    expect(control.preflight).toHaveBeenCalledTimes(1);
    expect(control.preflight).toHaveBeenCalledWith({
      tool: "match_best_tag", server: "insight-cube", args: { brand: "瑞幸咖啡" },
    });
  });
});
