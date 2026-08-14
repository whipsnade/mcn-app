import { afterEach, describe, expect, it, vi } from "vitest";
import { PiPocHttpClient } from "../src/http/client";

interface FetchCall {
  input: RequestInfo | URL;
  init: RequestInit;
}

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

describe("PiPocHttpClient", () => {
  it("token 只出现在 Authorization 头，绝不进入请求体", async () => {
    const fetchMock = mockFetchOnce(true, { call_id: "tracked-1" });
    const client = new PiPocHttpClient({
      baseUrl: "http://127.0.0.1:8000/api/v1/internal/pi-poc",
      runId: "run-1",
      token: "run-credential",
    });

    await client.startToolCall({
      toolCallId: "pi-tc-1",
      toolName: "kol_platform_search",
      requestedToolName: "social_grow_kol_platform_search",
      serviceName: "social-grow",
      arguments: { q: "咖啡" },
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const { input, init } = callOf(fetchMock);
    expect(String(input)).toBe(
      "http://127.0.0.1:8000/api/v1/internal/pi-poc/runs/run-1/tool-calls/start",
    );
    expect((init.headers as Record<string, string> | undefined)?.Authorization).toBe("Bearer run-credential");
    expect(String(init.body)).not.toContain("run-credential");
    expect(String(init.body)).not.toContain("token");
    expect(JSON.parse(String(init.body))).toMatchObject({
      tool_name: "kol_platform_search",
      requested_tool_name: "social_grow_kol_platform_search",
      service_name: "social-grow",
    });
  });

  it("settle 走正确路径且携带 raw_payload", async () => {
    const fetchMock = mockFetchOnce(true, { evidence_id: "ev-42" });
    const client = new PiPocHttpClient({
      baseUrl: "http://127.0.0.1:8000/api/v1/internal/pi-poc",
      runId: "run-1",
      token: "t",
    });

    const result = await client.settleToolCall("tracked-1", { data: [1], total: 1 });

    expect(result).toEqual({ evidenceId: "ev-42" });
    const { input } = callOf(fetchMock);
    expect(String(input)).toContain("/runs/run-1/tool-calls/tracked-1/settle");
  });

  it("fail 走错误路径", async () => {
    const fetchMock = mockFetchOnce(true, {});
    const client = new PiPocHttpClient({
      baseUrl: "http://127.0.0.1:8000/api/v1/internal/pi-poc",
      runId: "run-1",
      token: "t",
    });

    await client.failToolCall("tracked-1", { error: "gateway_timeout" });

    const { input } = callOf(fetchMock);
    expect(String(input)).toContain("/runs/run-1/tool-calls/tracked-1/fail");
  });

  it("Extension 阶段诊断只提交允许字段", async () => {
    const fetchMock = mockFetchOnce(true, { ok: true });
    const client = new PiPocHttpClient({
      baseUrl: "http://127.0.0.1:8000/api/v1/internal/pi-poc",
      runId: "run-1",
      token: "top-secret-run-token",
    });

    await client.recordExtensionDiagnostic({
      stage: "audit_start",
      serviceSlug: "insight-cube-mcp",
      toolName: "brand_search",
      exceptionType: "Error",
      errorCode: "fake_audit_start",
    });

    const { input, init } = callOf(fetchMock);
    expect(String(input)).toContain("/runs/run-1/diagnostics");
    expect(JSON.parse(String(init.body))).toEqual({
      stage: "audit_start",
      service_slug: "insight-cube-mcp",
      tool_name: "brand_search",
      exception_type: "Error",
      error_code: "fake_audit_start",
    });
    expect(String(init.body)).not.toContain("top-secret-run-token");
  });

  it("非 2xx 抛受控错误且不回显 token", async () => {
    const fetchMock = mockFetchOnce(false, { detail: "invalid_pi_poc_token" }, 401);
    const client = new PiPocHttpClient({
      baseUrl: "http://127.0.0.1:8000/api/v1/internal/pi-poc",
      runId: "run-1",
      token: "top-secret-run-token",
    });

    await expect(
      client.startToolCall({
        toolCallId: "c", toolName: "t", requestedToolName: "server_t", serviceName: "server", arguments: {},
      }),
    ).rejects.toThrow("pi_poc_http:401");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("非 2xx 不把响应正文拼接进错误消息", async () => {
    mockFetchOnce(false, { detail: "Bearer disallowed-value; server diagnostic" }, 500);
    const client = new PiPocHttpClient({
      baseUrl: "http://127.0.0.1:8000/api/v1/internal/pi-poc",
      runId: "run-1",
      token: "run-credential",
    });

    const message = await client
      .startToolCall({
        toolCallId: "c", toolName: "t", requestedToolName: "server_t", serviceName: "server", arguments: {},
      })
      .then(() => "unexpected_success")
      .catch((error: unknown) => (error instanceof Error ? error.message : "unknown"));

    expect(message).toBe("pi_poc_http:500");
  });
});
