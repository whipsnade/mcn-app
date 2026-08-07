import { describe, expect, it } from "vitest";
import { assertAuditTranscript, makeAuditRequests } from "../src/rpc/audit";
import { buildPiModelConfig } from "../src/rpc/model-config";
import { parseRpcLine } from "../src/rpc/protocol";

describe("parseRpcLine", () => {
  it("只接受单条 LF JSON 记录并保留字符串内 U+2028", () => {
    const record = parseRpcLine('{"type":"message_update","text":"a b"}\r');

    expect(record.type).toBe("message_update");
    expect(record.text).toBe("a b");
  });

  it("拒绝空行和非对象 JSON", () => {
    expect(() => parseRpcLine(" ")).toThrow("empty_rpc_record");
    expect(() => parseRpcLine("[]")).toThrow("invalid_rpc_record");
  });

  it("拒绝损坏 JSON 和缺少 type 的对象", () => {
    expect(() => parseRpcLine('{"type":')).toThrow(SyntaxError);
    expect(() => parseRpcLine('{"text":"missing type"}')).toThrow("invalid_rpc_record");
  });
});

describe("可审计 RPC 探针", () => {
  it("断言关联 id、response 形状和显式资源加载边界", () => {
    const requests = makeAuditRequests();
    const result = assertAuditTranscript(requests, [
      {
        id: "poc-get-state",
        type: "response",
        command: "get_state",
        success: true,
        data: { model: null, thinkingLevel: "medium" },
      },
      {
        id: "poc-get-commands",
        type: "response",
        command: "get_commands",
        success: true,
        data: {
          commands: [
            { name: "poc-explicit-extension", source: "extension" },
            { name: "skill:poc-explicit-skill", source: "skill", location: "path" },
          ],
        },
      },
      { id: "poc-abort", type: "response", command: "abort", success: true },
    ]);

    expect(result.stdoutJsonlOnly).toBe(true);
    expect(result.responseIds).toEqual(["poc-get-state", "poc-get-commands", "poc-abort"]);
    expect(result.explicitResources).toEqual([
      "poc-explicit-extension",
      "skill:poc-explicit-skill",
    ]);
  });

  it("拒绝缺失关联 id、失败 response 或自动发现资源", () => {
    const requests = makeAuditRequests();

    expect(() =>
      assertAuditTranscript(requests, [
        { type: "response", command: "get_state", success: true },
      ]),
    ).toThrow("invalid_rpc_response");

    expect(() =>
      assertAuditTranscript(requests, [
        { id: "poc-get-state", type: "response", command: "get_state", success: true },
        {
          id: "poc-get-commands",
          type: "response",
          command: "get_commands",
          success: true,
          data: {
            commands: [
              { name: "poc-explicit-extension", source: "extension" },
              { name: "skill:poc-explicit-skill", source: "skill" },
              { name: "poc-auto-extension", source: "extension" },
            ],
          },
        },
        { id: "poc-abort", type: "response", command: "abort", success: true },
      ]),
    ).toThrow("automatic_resource_loaded");
  });

  it("拒绝名称正确但资源来源被伪装的命令", () => {
    const requests = makeAuditRequests();

    expect(() =>
      assertAuditTranscript(requests, [
        { id: "poc-get-state", type: "response", command: "get_state", success: true },
        {
          id: "poc-get-commands",
          type: "response",
          command: "get_commands",
          success: true,
          data: {
            commands: [
              { name: "poc-explicit-extension", source: "extension" },
              { name: "skill:poc-explicit-skill", source: "extension" },
            ],
          },
        },
        { id: "poc-abort", type: "response", command: "abort", success: true },
      ]),
    ).toThrow("explicit_resource_missing");
  });

  it("只以环境变量名称生成临时同模型配置", () => {
    const config = buildPiModelConfig({
      baseUrl: "https://api.example.test/v1",
      model: "provider-model",
      thinking: "high",
      apiKeyPresent: true,
    });

    expect(config.providers.kol_insight_pi_poc.apiKey).toBe("$TENCENT_PLAN_API_KEY");
    expect(config.providers.kol_insight_pi_poc.models[0]).toMatchObject({
      id: "provider-model",
      thinkingLevelMap: { high: "high" },
    });
  });

  it("拒绝缺失同模型运行时配置", () => {
    expect(() =>
      buildPiModelConfig({
        baseUrl: "",
        model: "provider-model",
        thinking: "high",
        apiKeyPresent: true,
      }),
    ).toThrow("missing_runtime_model_settings");
  });
});
