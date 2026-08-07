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
      token: "top-secret-run-token",
    });

    await client.startToolCall({
      toolCallId: "pi-tc-1",
      toolName: "kol_platform_search",
      arguments: { q: "咖啡" },
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const { input, init } = callOf(fetchMock);
    expect(String(input)).toBe(
      "http://127.0.0.1:8000/api/v1/internal/pi-poc/runs/run-1/tool-calls/start",
    );
    expect((init.headers as Record<string, string> | undefined)?.Authorization).toBe("Bearer top-secret-run-token");
    expect(String(init.body)).not.toContain("top-secret-run-token");
    expect(String(init.body)).not.toContain("token");
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

  it("非 2xx 抛受控错误且不回显 token", async () => {
    const fetchMock = mockFetchOnce(false, { detail: "invalid_pi_poc_token" }, 401);
    const client = new PiPocHttpClient({
      baseUrl: "http://127.0.0.1:8000/api/v1/internal/pi-poc",
      runId: "run-1",
      token: "top-secret-run-token",
    });

    await expect(
      client.startToolCall({ toolCallId: "c", toolName: "t", arguments: {} }),
    ).rejects.toThrow("pi_poc_http:401");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
