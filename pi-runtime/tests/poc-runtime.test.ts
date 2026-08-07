import { describe, expect, it } from "vitest";

import { readRuntimeConfigFromEnv } from "../src/extensions/poc-runtime";

describe("Pi POC 多 DataTap MCP 配置", () => {
  it("只接受显式的服务到 endpoint 映射", () => {
    const config = readRuntimeConfigFromEnv({
      DATATAP_MCP_ENDPOINTS_JSON: JSON.stringify({
        "insight-cube-mcp": "https://datatap.example.test/insight/mcp",
        "social-grow-mcp": "https://datatap.example.test/kol/mcp",
      }),
      DATATAP_MCP_TOKEN: "test-only-token",
      PI_RUNTIME_POC_BASE_URL: "http://127.0.0.1:8000/api/v1/internal/pi-poc",
      PI_RUNTIME_POC_RUN_ID: "run-1",
      PI_RUNTIME_POC_TOKEN: "run-token",
    });

    expect(config.datatapEndpoints).toEqual({
      "insight-cube-mcp": "https://datatap.example.test/insight/mcp",
      "social-grow-mcp": "https://datatap.example.test/kol/mcp",
    });
  });

  it("拒绝缺失或非 URL 的 endpoint 映射", () => {
    expect(() => readRuntimeConfigFromEnv({ DATATAP_MCP_ENDPOINTS_JSON: "{}" })).toThrow(
      "pi_poc_invalid_datatap_endpoints",
    );
    expect(() =>
      readRuntimeConfigFromEnv({
        DATATAP_MCP_ENDPOINTS_JSON: '{"insight-cube-mcp":"not-a-url"}',
      }),
    ).toThrow("pi_poc_invalid_datatap_endpoints");
  });
});
