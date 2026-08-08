import { afterEach, describe, expect, it, vi } from "vitest";
import { PiPocHttpClient } from "../src/http/client";
import {
  PI_INTERNAL_TOOL_NAMES,
  PiInternalToolsClient,
  isAllowedInternalTool,
  registerInternalTools,
} from "../src/extensions/internal-tools";

function mockFetchOnce(ok: boolean, body: unknown, status = ok ? 200 : 404) {
  const response = {
    ok,
    status,
    json: vi.fn(async () => body),
    text: vi.fn(async () => JSON.stringify(body)),
  };
  const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => response);
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

interface FetchCall {
  input: RequestInfo | URL;
  init: RequestInit;
}

function callOf(fetchMock: ReturnType<typeof mockFetchOnce>): FetchCall {
  const entry = fetchMock.mock.calls[0];
  if (entry === undefined) {
    throw new Error("fetch was not called");
  }
  return { input: entry[0], init: entry[1] ?? {} };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

function makeClient(): PiInternalToolsClient {
  const http = new PiPocHttpClient({
    baseUrl: "http://127.0.0.1:8000/api/v1/internal/pi-poc",
    runId: "run-1",
    token: "run-token",
  });
  return new PiInternalToolsClient(http);
}

describe("isAllowedInternalTool", () => {
  it("只允许设计白名单内的受控内部工具", () => {
    expect(PI_INTERNAL_TOOL_NAMES).toEqual([
      "get_session_context",
      "load_marketing_skill",
      "search_evidence",
      "read_tool_result",
      "read_artifact",
      "build_brand_report_draft",
      "build_campaign_report_draft",
      "build_kol_selection_draft",
      "build_kol_analysis_draft",
      "build_kol_detail_draft",
      "build_insight_draft",
      "publish_artifacts",
      "request_clarification",
    ]);
    for (const name of PI_INTERNAL_TOOL_NAMES) {
      expect(isAllowedInternalTool(name)).toBe(true);
    }
  });

  it("拒绝 bash/shell/文件编辑/任意 HTTP/Draft 直写/计算/记忆等越权工具", () => {
    for (const name of [
      "bash",
      "shell",
      "read",
      "write",
      "edit",
      "ls",
      "curl",
      "http",
      "fetch",
      "create_draft",
      "update_draft",
      "abandon_draft",
      "remember_scope",
      "calculate_expression",
      "aggregate_metrics",
      "rank_kols",
    ]) {
      expect(isAllowedInternalTool(name)).toBe(false);
    }
  });
});

describe("PiInternalToolsClient.execute", () => {
  it("把工具名与参数原样发往内部端点，body 不含任何伪造身份键", async () => {
    const fetchMock = mockFetchOnce(true, { result: { ok: true } });
    const client = makeClient();

    const result = await client.execute("get_session_context", {
      user_id: "forged-user",
      session_id: "forged-session",
      run_id: "forged-run",
      worker_id: "forged-worker",
      arguments: { x: 1 },
    });

    expect(result).toEqual({ ok: true });
    const { input, init } = callOf(fetchMock);
    expect(String(input)).toBe(
      "http://127.0.0.1:8000/api/v1/internal/pi-poc/runs/run-1/internal-tools",
    );
    const sent = JSON.parse(String(init.body));
    expect(sent.tool_name).toBe("get_session_context");
    expect(sent.arguments).toEqual({
      user_id: "forged-user",
      session_id: "forged-session",
      run_id: "forged-run",
      worker_id: "forged-worker",
      arguments: { x: 1 },
    });
  });

  it("拒绝白名单外的工具，不发任何网络请求", async () => {
    const fetchMock = mockFetchOnce(true, { result: {} });
    const client = makeClient();

    await expect(client.execute("bash", {})).rejects.toThrow("pi_internal_tool_not_allowed");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("executeInternalTool 走正确路径且 token 只在 Authorization", async () => {
    const fetchMock = mockFetchOnce(true, { result: { evidence: [] } });
    const http = new PiPocHttpClient({
      baseUrl: "http://127.0.0.1:8000/api/v1/internal/pi-poc",
      runId: "run-9",
      token: "run-credential",
    });

    await http.executeInternalTool("search_evidence", { query: "咖啡" });

    const { input, init } = callOf(fetchMock);
    expect(String(input)).toContain("/runs/run-9/internal-tools");
    expect((init.headers as Record<string, string> | undefined)?.Authorization).toBe(
      "Bearer run-credential",
    );
    const sent = JSON.parse(String(init.body));
    expect(sent.tool_name).toBe("search_evidence");
    expect(String(init.body)).not.toContain("run-credential");
  });
});

describe("registerInternalTools", () => {
  it("只向 Pi 注册受控白名单，并将返回值原样封装为工具文本", async () => {
    const registered: Array<{
      name: string;
      description: string;
      parameters: { required?: string[] };
      execute: (toolCallId: string, args: Record<string, unknown>) => Promise<unknown>;
    }> = [];
    const pi = { registerTool: (tool: (typeof registered)[number]) => registered.push(tool) };
    const http = { executeInternalTool: vi.fn(async () => ({ draft_id: "draft-1" })) };

    registerInternalTools(pi as never, new PiInternalToolsClient(http as never));

    expect(registered.map((tool) => tool.name)).toEqual(PI_INTERNAL_TOOL_NAMES);
    const result = await registered[0]!.execute("pi-call-1", { ignored: false });
    expect(http.executeInternalTool).toHaveBeenCalledWith("get_session_context", { ignored: false });
    expect(result).toEqual({
      content: [{ type: "text", text: '{"draft_id":"draft-1"}' }],
      details: {},
    });
  });

  it("为需要定位对象的内部工具注册精确参数契约与独立说明", () => {
    const registered: Array<{
      name: string;
      description: string;
      parameters: { required?: string[] };
      execute: (toolCallId: string, args: Record<string, unknown>) => Promise<unknown>;
    }> = [];
    const pi = { registerTool: (tool: (typeof registered)[number]) => registered.push(tool) };
    const http = { executeInternalTool: vi.fn() };

    registerInternalTools(pi as never, new PiInternalToolsClient(http as never));

    const byName = (name: string) => registered.find((tool) => tool.name === name)!;
    expect(byName("request_clarification").parameters.required).toEqual(["question"]);
    expect(byName("read_artifact").parameters.required).toEqual(["artifact_id"]);
    expect(byName("read_tool_result").parameters.required).toEqual(["evidence_id"]);
    expect(new Set(registered.map((tool) => tool.description)).size).toBeGreaterThan(1);
  });
});
