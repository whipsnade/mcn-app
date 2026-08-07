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
import { PiPocHttpClient } from "../http/client.js";

export interface PlayerRuntimeConfig {
  datatapUrl: string;
  datatapToken: string;
  baseUrl: string;
  runId: string;
  runToken: string;
}

export function readRuntimeConfigFromEnv(env: NodeJS.ProcessEnv = process.env): PlayerRuntimeConfig {
  return {
    datatapUrl: env.DATATAP_MCP_URL ?? "",
    datatapToken: env.DATATAP_MCP_TOKEN ?? "",
    baseUrl: env.PI_RUNTIME_POC_BASE_URL ?? "",
    runId: env.PI_RUNTIME_POC_RUN_ID ?? "",
    runToken: env.PI_RUNTIME_POC_TOKEN ?? "",
  };
}

export function createDatatapMcpClient(opts: PlayerRuntimeConfig): McpToolClient {
  const transport = new StreamableHTTPClientTransport(new URL(opts.datatapUrl), {
    requestInit: { headers: { Authorization: `Bearer ${opts.datatapToken}` } },
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
  if (!env.datatapUrl || !env.datatapToken) {
    throw new Error("pi_poc_missing_datatap_config");
  }
  const mcp = createDatatapMcpClient(env);

  const audit = new PiPocHttpClient({
    baseUrl: env.baseUrl,
    runId: env.runId,
    token: env.runToken,
  });

  const tools = await discoverDatatapTools(mcp);
  for (const tool of tools) {
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
        });
        return {
          content: [{ type: "text", text: JSON.stringify(payload) }],
          details: {},
        };
      },
    });
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
