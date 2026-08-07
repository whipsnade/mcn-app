/**
 * Pi POC Runtime 组合入口（薄壳）。
 *
 * 职责：连接 DataTap MCP → 原样发现工具目录 → 逐个注册为 Pi 工具；每个工具
 * execute 时经 `callDatatapTransparent` 做透明转发 + 内部审计旁路。不持有任何
 * token，token 由调用进程以环境变量注入（Task 7 runner 负责显式注入，本模块
 * 只读取 process.env，绝不把 token 写入任何输出）。
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { Type } from "typebox";

import {
  callDatatapTransparent,
  discoverDatatapTools,
  type DatatapToolDescriptor,
  type McpCallOutcome,
  type McpToolClient,
} from "./datatap-mcp.js";
import { PiInternalToolsClient, registerInternalTools } from "./internal-tools.js";
import { PiPocHttpClient } from "../http/client.js";
import { redact } from "../redaction.js";

export interface PlayerRuntimeConfig {
  datatapEndpoints: Record<string, string>;
  datatapToken: string;
  baseUrl: string;
  runId: string;
  runToken: string;
}

export function readRuntimeConfigFromEnv(env: NodeJS.ProcessEnv = process.env): PlayerRuntimeConfig {
  const datatapEndpoints = parseDatatapEndpoints(env.DATATAP_MCP_ENDPOINTS_JSON);
  return {
    datatapEndpoints,
    datatapToken: env.DATATAP_MCP_TOKEN ?? "",
    baseUrl: env.PI_RUNTIME_POC_BASE_URL ?? "",
    runId: env.PI_RUNTIME_POC_RUN_ID ?? "",
    runToken: env.PI_RUNTIME_POC_TOKEN ?? "",
  };
}

export function createDatatapMcpClient(url: string, token: string): McpToolClient {
  const transport = new StreamableHTTPClientTransport(new URL(url), {
    // DataTap 接入链接的 generic MCP 配置明确以 DATATAP_TOKEN 作为 bearer 凭证；
    // token 仅由调用进程临时注入，绝不进入工具参数、审计或输出。
    requestInit: { headers: { Authorization: `Bearer ${token}` } },
  });
  const client = new Client({ name: "kol_insight_pi_poc", version: "0.0.0" });

  return {
    async listTools(): Promise<DatatapToolDescriptor[]> {
      await client.connect(transport);
      const result = await client.listTools();
      return result.tools.map((tool) => ({
        name: tool.name,
        description: tool.description,
        inputSchema: tool.inputSchema,
      }));
    },
    async callTool(name: string, args: Record<string, unknown>): Promise<McpCallOutcome> {
      const result = await client.callTool({ name, arguments: args });
      const isError = result.isError === true;
      return {
        content: result.content,
        isError,
        error: isError ? extractErrorText(result.content) : undefined,
      };
    },
  };
}

export default async function (pi: ExtensionAPI): Promise<void> {
  const env = readRuntimeConfigFromEnv();
  if (!env.datatapToken) {
    throw new Error("pi_poc_missing_datatap_config");
  }
  const audit = new PiPocHttpClient({
    baseUrl: env.baseUrl,
    runId: env.runId,
    token: env.runToken,
  });
  registerInternalTools(pi, new PiInternalToolsClient(audit));

  const registeredToolNames = new Set<string>();
  for (const url of Object.values(env.datatapEndpoints)) {
    const mcp = createDatatapMcpClient(url, env.datatapToken);
    const tools = await discoverDatatapTools(mcp);
    for (const tool of tools) {
      // 不允许以 service 前缀重命名或意图路由来掩盖冲突；重名即让整个 Run 失败关闭。
      if (registeredToolNames.has(tool.name)) {
        throw new Error("pi_poc_duplicate_datatap_tool_name");
      }
      registeredToolNames.add(tool.name);
      pi.registerTool({
        name: tool.name,
        label: tool.name,
        description: tool.description ?? "",
        parameters: Type.Object({}, { additionalProperties: Type.Unknown() }),
        execute: async (toolCallId, params) => {
          const { payload } = await callDatatapTransparent({
            mcp,
            audit,
            toolCallId,
            toolName: tool.name,
            arguments: (params ?? {}) as Record<string, unknown>,
            redactAudit: (value) => redact(value, [env.datatapToken, env.runToken]),
          });
          return {
            content: [{ type: "text", text: JSON.stringify(payload) }],
            details: {},
          };
        },
      });
    }
  }
}

function parseDatatapEndpoints(value: string | undefined): Record<string, string> {
  if (!value) {
    throw new Error("pi_poc_invalid_datatap_endpoints");
  }
  try {
    const parsed: unknown = JSON.parse(value);
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error("invalid");
    }
    const entries = Object.entries(parsed);
    if (entries.length === 0 || entries.some(([slug, url]) => !slug || typeof url !== "string")) {
      throw new Error("invalid");
    }
    for (const [, url] of entries) {
      const parsedUrl = new URL(url as string);
      if (parsedUrl.protocol !== "https:" && parsedUrl.protocol !== "http:") {
        throw new Error("invalid");
      }
    }
    return Object.fromEntries(entries) as Record<string, string>;
  } catch {
    throw new Error("pi_poc_invalid_datatap_endpoints");
  }
}

function extractErrorText(content: unknown): string | undefined {
  if (Array.isArray(content)) {
    for (const part of content) {
      if (part && typeof part === "object" && (part as { type?: unknown }).type === "text") {
        const text = (part as { text?: unknown }).text;
        if (typeof text === "string" && text) {
          return text;
        }
      }
    }
  }
  return undefined;
}
