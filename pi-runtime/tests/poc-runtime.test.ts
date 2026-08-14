import { describe, expect, it } from "vitest";

import { readRuntimeConfigFromEnv } from "../src/extensions/poc-runtime.js";

const validEnvironment = {
  DATATAP_INSIGHT_CUBE_MCP_URL: "https://datatap.example.test/insight/mcp",
  DATATAP_SOCIAL_GROW_MCP_URL: "https://datatap.example.test/social/mcp",
  DATATAP_SOCIAL_GROW_CONTENT_MCP_URL: "https://datatap.example.test/content/mcp",
  DATATAP_AKTOOLS_MCP_URL: "https://datatap.example.test/aktools/mcp",
  DATATAP_MCP_TOKEN: "test-only-token",
  PI_RUNTIME_POC_BASE_URL: "http://127.0.0.1:8000/api/v1/internal/pi-poc",
  PI_RUNTIME_POC_RUN_ID: "run-1",
  PI_RUNTIME_POC_TOKEN: "run-token",
};

describe("Pi POC adapter 审计配置", () => {
  it("仅接受全部四服务 URL 与受控审计上下文", () => {
    expect(readRuntimeConfigFromEnv(validEnvironment)).toEqual({
      baseUrl: validEnvironment.PI_RUNTIME_POC_BASE_URL,
      runId: "run-1",
      runToken: "run-token",
    });
  });

  it("缺少任一 adapter URL 时在真实请求前 fail-closed", () => {
    const environment: NodeJS.ProcessEnv = { ...validEnvironment };
    delete environment.DATATAP_AKTOOLS_MCP_URL;
    expect(() => readRuntimeConfigFromEnv(environment)).toThrow("pi_poc_adapter_datatap_url_required");
  });

  it("拒绝非 HTTP DataTap URL", () => {
    expect(() => readRuntimeConfigFromEnv({ ...validEnvironment, DATATAP_AKTOOLS_MCP_URL: "file:///tmp/x" }))
      .toThrow("pi_poc_adapter_datatap_url_required");
  });
});
