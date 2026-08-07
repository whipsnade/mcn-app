import { describe, expect, it, vi } from "vitest";
import {
  callDatatapTransparent,
  discoverDatatapTools,
  type McpCallOutcome,
  type McpToolClient,
  type ToolAuditClient,
} from "../src/extensions/datatap-mcp";

function makeMcp(overrides: Partial<McpToolClient> = {}): McpToolClient & {
  calls: Array<{ name: string; args: Record<string, unknown> }>;
} {
  const calls: Array<{ name: string; args: Record<string, unknown> }> = [];
  return {
    listTools: vi.fn(async () => [
      { name: "kol_platform_search", description: "search", inputSchema: { type: "object" } },
    ]),
    callTool: vi.fn(async (name: string, args: Record<string, unknown>) => {
      calls.push({ name, args });
      return { content: { data: [1, 2], total: 2 }, isError: false };
    }),
    ...overrides,
    calls,
  } as McpToolClient & { calls: Array<{ name: string; args: Record<string, unknown> }> };
}

function makeAudit(overrides: Partial<ToolAuditClient> = {}) {
  const started: unknown[] = [];
  const settled: unknown[] = [];
  const failed: unknown[] = [];
  return {
    startToolCall: vi.fn(async (call: unknown) => {
      started.push(call);
      return { trackedCallId: "tracked-1" };
    }),
    settleToolCall: vi.fn(async (_tracked: string, raw: unknown) => {
      settled.push(raw);
      return { evidenceId: "ev-1" };
    }),
    failToolCall: vi.fn(async (_tracked: string, error: unknown) => {
      failed.push(error);
      return undefined;
    }),
    ...overrides,
    started,
    settled,
    failed,
  };
}

describe("callDatatapTransparent", () => {
  it("发往 MCP 的 tool name/arguments 与 Pi 输入深相等", async () => {
    const mcp = makeMcp();
    const audit = makeAudit();
    const args = { keywords: "咖啡", platform: "xiaohongshu" };

    await callDatatapTransparent({
      mcp,
      audit,
      toolCallId: "pi-tc-1",
      toolName: "kol_platform_search",
      arguments: args,
    });

    expect(mcp.calls).toEqual([{ name: "kol_platform_search", args }]);
    expect(mcp.calls[0].args).toEqual(args);
  });

  it("返回 Pi 的业务 payload 与 MCP 响应深相等，仅新增顶层 _runtime_metadata", async () => {
    const mcp = makeMcp({
      callTool: vi.fn(async () => ({ content: { data: [1, 2], total: 2 }, isError: false })),
    });
    const audit = makeAudit();

    const { payload } = await callDatatapTransparent({
      mcp,
      audit,
      toolCallId: "pi-tc-1",
      toolName: "kol_platform_search",
      arguments: {},
    });

    expect(payload).toEqual({
      data: [1, 2],
      total: 2,
      _runtime_metadata: {
        toolCallId: "pi-tc-1",
        trackedCallId: "tracked-1",
        evidenceId: "ev-1",
        isError: false,
      },
    });
  });

  it("每条工具调用只发起一次 MCP call（settle 走审计不走 MCP）", async () => {
    const mcp = makeMcp();
    const audit = makeAudit();

    await callDatatapTransparent({
      mcp,
      audit,
      toolCallId: "pi-tc-1",
      toolName: "kol_platform_search",
      arguments: {},
    });

    expect(mcp.calls).toHaveLength(1);
    expect(audit.settleToolCall).toHaveBeenCalledTimes(1);
  });

  it("错误/超时不改写 error code/message", async () => {
    const mcp = makeMcp({
      callTool: vi.fn(async () => ({
        content: { error: "gateway_timeout", message: "upstream slow" },
        isError: true,
        error: "gateway_timeout: upstream slow",
      })),
    });
    const audit = makeAudit();

    const { payload, metadata } = await callDatatapTransparent({
      mcp,
      audit,
      toolCallId: "pi-tc-1",
      toolName: "kol_platform_search",
      arguments: {},
    });

    expect(metadata.isError).toBe(true);
    expect(metadata.error).toBe("gateway_timeout: upstream slow");
    expect(audit.failToolCall).toHaveBeenCalledWith("tracked-1", {
      error: "gateway_timeout: upstream slow",
    });
    expect(payload).toEqual({
      error: "gateway_timeout",
      message: "upstream slow",
      _runtime_metadata: expect.objectContaining({ isError: true }),
    });
    expect(audit.settleToolCall).not.toHaveBeenCalled();
  });

  it("MCP 抛异常时先旁路登记 failed，再原样抛回 Pi", async () => {
    const upstream = new Error("gateway_timeout: upstream slow");
    const mcp = makeMcp({ callTool: vi.fn(async () => { throw upstream; }) });
    const audit = makeAudit();

    await expect(callDatatapTransparent({
      mcp, audit, toolCallId: "pi-tc-1", toolName: "kol_platform_search", arguments: {},
    })).rejects.toBe(upstream);
    expect(audit.failToolCall).toHaveBeenCalledWith("tracked-1", { error: upstream });
  });

  it("只脱敏审计错误，返回 Pi 的原始错误对象不变", async () => {
    const raw = { message: "token=abc123" };
    const mcp = makeMcp({ callTool: vi.fn(async () => ({ content: raw, isError: true, error: raw.message })) });
    const audit = makeAudit();

    const result = await callDatatapTransparent({
      mcp, audit, toolCallId: "pi-tc-1", toolName: "kol_platform_search", arguments: {},
      redactAudit: (value) => JSON.parse(JSON.stringify(value).replaceAll("abc123", "***")),
    });
    expect(audit.failToolCall).toHaveBeenCalledWith("tracked-1", { error: "token=***" });
    expect(result.payload).toMatchObject({ message: "token=abc123" });
  });

  it("token 不进入 audit body（审计只收业务输入，token 只在 HTTP Authorization）", async () => {
    const mcp = makeMcp();
    const audit = makeAudit();

    await callDatatapTransparent({
      mcp,
      audit,
      toolCallId: "pi-tc-1",
      toolName: "kol_platform_search",
      arguments: { query: "n" },
    });

    const bodies = [...audit.started, ...audit.settled, ...audit.failed];
    const serialized = JSON.stringify(bodies);
    expect(serialized).not.toContain("Bearer");
    expect(serialized).not.toContain("token");
  });
});

describe("discoverDatatapTools", () => {
  it("原样返回 DataTap 工具目录，不裁剪", async () => {
    const mcp = makeMcp();
    const tools = await discoverDatatapTools(mcp);
    expect(tools).toEqual([
      { name: "kol_platform_search", description: "search", inputSchema: { type: "object" } },
    ]);
  });
});
