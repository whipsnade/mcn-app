import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

import { describe, expect, it, vi } from "vitest";

import { installPocAuditExtension } from "../src/extensions/poc-runtime.js";

describe("Pi MCP Adapter 项目配置", () => {
  it("使用标准项目 .mcp.json，且只引用环境变量", async () => {
    const raw = await readFile(resolve(".mcp.json"), "utf8");
    const config = JSON.parse(raw) as Record<string, unknown>;

    expect(config).toMatchObject({
      settings: {
        hostConfigDiscovery: "off",
        scriptMode: false,
        directTools: false,
        requestTimeoutMs: 180000,
        outputGuard: false,
      },
    });
    expect(config).not.toContain("Bearer sk-");
    expect(config).not.toContain("datatap.deepminer.com.cn/api/connect/");
    expect(Object.keys(config.mcpServers as Record<string, unknown>)).toEqual([
      "insight-cube",
      "social-grow",
      "social-grow-content",
      "aktools",
    ]);
  });
});

describe("Pi MCP 审计旁路", () => {
  it("只在 adapter 确认真实 MCP 成功后创建 ToolCall，并保存请求名与原始名", async () => {
    const handlers = new Map<string, (event: any) => Promise<unknown>>();
    const pi = { on: vi.fn((name: string, handler: (event: any) => Promise<unknown>) => handlers.set(name, handler)) };
    const audit = {
      startToolCall: vi.fn(async () => ({ trackedCallId: "tracked-1" })),
      settleToolCall: vi.fn(async () => ({ evidenceId: "evidence-1" })),
      failToolCall: vi.fn(async () => undefined),
      recordExtensionDiagnostic: vi.fn(async () => undefined),
    };

    installPocAuditExtension(pi as never, audit as never);
    await handlers.get("tool_call")!({
      type: "tool_call", toolCallId: "tool-call-1", toolName: "mcp",
      input: { tool: "insight_cube_query_analysis_data", args: { period: "7d" }, server: "insight-cube" },
    });
    expect(audit.startToolCall).not.toHaveBeenCalled();
    await handlers.get("tool_result")!({
      type: "tool_result", toolCallId: "tool-call-1", toolName: "mcp",
      input: { tool: "insight_cube_query_analysis_data", args: { period: "7d" }, server: "insight-cube" },
      content: [{ type: "text", text: "raw-result" }], isError: false,
      details: { mode: "call", server: "insight-cube", tool: "query_analysis_data", mcpResult: { raw: true } },
    });

    expect(audit.startToolCall).toHaveBeenCalledTimes(1);
    expect(audit.startToolCall).toHaveBeenCalledWith({
      toolCallId: "tool-call-1",
      toolName: "query_analysis_data",
      requestedToolName: "insight_cube_query_analysis_data",
      serviceName: "insight-cube",
      arguments: { period: "7d" },
    });
    expect(audit.settleToolCall).toHaveBeenCalledWith("tracked-1", { raw: true });
    expect(audit.failToolCall).not.toHaveBeenCalled();
  });

  it("目录搜索不创建 ToolCall，审计落库失败不阻断 adapter 调用", async () => {
    const handlers = new Map<string, (event: any) => Promise<unknown>>();
    const pi = { on: vi.fn((name: string, handler: (event: any) => Promise<unknown>) => handlers.set(name, handler)) };
    const audit = {
      startToolCall: vi.fn(async () => { throw new Error("pi_poc_audit_start_failed"); }),
      settleToolCall: vi.fn(async () => ({ evidenceId: "unused" })),
      failToolCall: vi.fn(async () => undefined),
      recordExtensionDiagnostic: vi.fn(async () => undefined),
    };

    installPocAuditExtension(pi as never, audit as never);
    expect(await handlers.get("tool_call")!({
      type: "tool_call", toolCallId: "search-1", toolName: "mcp", input: { search: "KOL" },
    })).toBeUndefined();
    const before = await handlers.get("tool_call")!({
      type: "tool_call", toolCallId: "call-1", toolName: "mcp",
      input: { tool: "insight_cube_query_analysis_data", args: {}, server: "insight-cube" },
    });
    await handlers.get("tool_result")!({
      type: "tool_result", toolCallId: "call-1", toolName: "mcp", isError: false,
      input: { tool: "insight_cube_query_analysis_data", args: {}, server: "insight-cube" },
      content: [{ type: "text", text: "raw-result" }],
      details: { mode: "call", server: "insight-cube", tool: "query_analysis_data", mcpResult: { raw: true } },
    });

    expect(audit.startToolCall).toHaveBeenCalledTimes(1);
    expect(before).toBeUndefined();
    expect(audit.settleToolCall).not.toHaveBeenCalled();
    expect(audit.recordExtensionDiagnostic).toHaveBeenCalledWith({
      stage: "audit_settle", serviceSlug: "insight-cube-mcp", toolName: "query_analysis_data",
      exceptionType: "Error", errorCode: "pi_poc_audit_start_failed",
    });
  });

  it("adapter 返回 MCP 业务错误时只失败收口，不能生成成功 Evidence", async () => {
    const handlers = new Map<string, (event: any) => Promise<unknown>>();
    const pi = { on: vi.fn((name: string, handler: (event: any) => Promise<unknown>) => handlers.set(name, handler)) };
    const audit = {
      startToolCall: vi.fn(async () => ({ trackedCallId: "tracked-error" })),
      settleToolCall: vi.fn(async () => ({ evidenceId: "must-not-exist" })),
      failToolCall: vi.fn(async () => undefined),
      recordExtensionDiagnostic: vi.fn(async () => undefined),
    };

    installPocAuditExtension(pi as never, audit as never);
    await handlers.get("tool_call")!({
      type: "tool_call", toolCallId: "adapter-error-1", toolName: "mcp",
      input: { tool: "social_grow_content_hotwords_xiaohongshu_dictionary", args: {}, server: "social-grow-content" },
    });
    expect(audit.startToolCall).not.toHaveBeenCalled();
    await handlers.get("tool_result")!({
      type: "tool_result", toolCallId: "adapter-error-1", toolName: "mcp", isError: false,
      content: [{ type: "text", text: "provider error" }],
      details: {
        mode: "call", error: "tool_error", server: "social-grow-content", tool: "hotwords_xiaohongshu_dictionary",
        mcpResult: { isError: true, content: [{ type: "text", text: "provider error" }] },
      },
    });

    expect(audit.failToolCall).toHaveBeenCalledWith("tracked-error", {
      isError: true, content: [{ type: "text", text: "provider error" }],
    });
    expect(audit.settleToolCall).not.toHaveBeenCalled();
  });

  it("裸名未解析为真实 MCP 请求时只保留失败尝试，不创建 ToolCall 或 Evidence", async () => {
    const handlers = new Map<string, (event: any) => Promise<unknown>>();
    const pi = { on: vi.fn((name: string, handler: (event: any) => Promise<unknown>) => handlers.set(name, handler)) };
    const audit = {
      startToolCall: vi.fn(async () => ({ trackedCallId: "must-not-exist" })),
      settleToolCall: vi.fn(async () => ({ evidenceId: "must-not-exist" })),
      failToolCall: vi.fn(async () => undefined),
      recordExtensionDiagnostic: vi.fn(async () => undefined),
    };

    installPocAuditExtension(pi as never, audit as never);
    await handlers.get("tool_call")!({
      type: "tool_call", toolCallId: "bare-tool-1", toolName: "mcp",
      input: { tool: "hotwords_xiaohongshu_dictionary", args: {}, server: "social-grow-content" },
    });
    await handlers.get("tool_result")!({
      type: "tool_result", toolCallId: "bare-tool-1", toolName: "mcp", isError: false,
      content: [{ type: "text", text: "not found" }],
      details: { mode: "call", error: "tool_not_found", server: "social-grow-content", requestedTool: "hotwords_xiaohongshu_dictionary" },
    });

    expect(audit.startToolCall).not.toHaveBeenCalled();
    expect(audit.settleToolCall).not.toHaveBeenCalled();
    expect(audit.failToolCall).not.toHaveBeenCalled();
    expect(audit.recordExtensionDiagnostic).toHaveBeenCalledWith({
      stage: "mcp_call", serviceSlug: "social-grow-content-mcp", toolName: "hotwords_xiaohongshu_dictionary",
      errorCode: "tool_not_found",
    });
  });
});
