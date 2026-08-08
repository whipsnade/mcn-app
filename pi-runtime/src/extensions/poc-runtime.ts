/**
 * Pi POC 项目扩展：只负责内部工具与 DataTap MCP 代理调用的审计旁路。
 *
 * DataTap 的连接、发现、代理调用均由显式加载的 pi-mcp-adapter 处理；本文件
 * 不导入 MCP SDK、不注册 DataTap 工具、不修改 Pi 的业务输入/输出。
 */

import type { ExtensionAPI, ToolCallEvent, ToolResultEvent } from "@earendil-works/pi-coding-agent";

import { PiInternalToolsClient, registerInternalTools } from "./internal-tools.js";
import { PiPocHttpClient, type PiExtensionDiagnostic } from "../http/client.js";

const REQUIRED_DATATAP_URLS = [
  "DATATAP_INSIGHT_CUBE_MCP_URL",
  "DATATAP_SOCIAL_GROW_MCP_URL",
  "DATATAP_SOCIAL_GROW_CONTENT_MCP_URL",
  "DATATAP_AKTOOLS_MCP_URL",
] as const;

const SERVICE_SLUGS: Record<string, string> = {
  "insight-cube": "insight-cube-mcp",
  "social-grow": "social-grow-mcp",
  "social-grow-content": "social-grow-content-mcp",
  aktools: "bilibili-mcp",
};

type AuditClient = Pick<
  PiPocHttpClient,
  "startToolCall" | "settleToolCall" | "failToolCall" | "recordExtensionDiagnostic"
>;

type TrackedCall = {
  requestedToolName: string;
  arguments: Record<string, unknown>;
  requestedServiceName?: string;
};

export interface PocAuditRuntimeConfig {
  baseUrl: string;
  runId: string;
  runToken: string;
}

export function readRuntimeConfigFromEnv(env: NodeJS.ProcessEnv = process.env): PocAuditRuntimeConfig {
  for (const key of REQUIRED_DATATAP_URLS) {
    const value = env[key]?.trim();
    if (!value || !isHttpUrl(value)) throw new Error("pi_poc_adapter_datatap_url_required");
  }
  if (!env.DATATAP_MCP_TOKEN?.trim()) throw new Error("pi_poc_missing_datatap_config");
  const baseUrl = env.PI_RUNTIME_POC_BASE_URL?.trim();
  const runId = env.PI_RUNTIME_POC_RUN_ID?.trim();
  const runToken = env.PI_RUNTIME_POC_TOKEN?.trim();
  if (!baseUrl || !isHttpUrl(baseUrl) || !runId || !runToken) {
    throw new Error("pi_poc_audit_config_required");
  }
  return { baseUrl, runId, runToken };
}

export default async function pocRuntime(pi: ExtensionAPI): Promise<void> {
  let config: PocAuditRuntimeConfig;
  const provisionalAudit = new PiPocHttpClient({
    baseUrl: process.env.PI_RUNTIME_POC_BASE_URL ?? "",
    runId: process.env.PI_RUNTIME_POC_RUN_ID ?? "",
    token: process.env.PI_RUNTIME_POC_TOKEN ?? "",
  });
  try {
    config = readRuntimeConfigFromEnv();
  } catch (error) {
    await reportDiagnostic(provisionalAudit, failureDiagnostic("config", error));
    throw error;
  }

  const audit = new PiPocHttpClient({ baseUrl: config.baseUrl, runId: config.runId, token: config.runToken });
  try {
    registerInternalTools(pi, new PiInternalToolsClient(audit));
  } catch (error) {
    await reportDiagnostic(audit, failureDiagnostic("tool_register", error));
    throw error;
  }
  installPocAuditExtension(pi, audit);
}

/** 安装不可变审计旁路；仅在 adapter 的单一真实 mcp 调用上记录。 */
export function installPocAuditExtension(pi: ExtensionAPI, audit: AuditClient): void {
  const tracked = new Map<string, TrackedCall>();

  pi.on("tool_call", async (event: ToolCallEvent) => {
    const call = extractMcpCall(event);
    if (!call) return undefined;
    tracked.set(event.toolCallId, call);
    return undefined;
  });

  pi.on("tool_result", async (event: ToolResultEvent) => {
    const call = tracked.get(event.toolCallId);
    if (!call) return undefined;
    tracked.delete(event.toolCallId);
    const adapterResult = parseAdapterResult(event, call);
    if (adapterResult === null) return undefined;
    if (adapterResult.errorCode) {
      await reportDiagnostic(audit, {
        stage: "mcp_call",
        serviceSlug: adapterResult.serviceSlug,
        toolName: call.requestedToolName,
        errorCode: adapterResult.errorCode,
      });
    }
    if (!adapterResult.mcpResult || !adapterResult.originalToolName || !adapterResult.serviceName) {
      return undefined;
    }
    try {
      await reportDiagnostic(audit, {
        stage: "mcp_call", serviceSlug: adapterResult.serviceSlug, toolName: adapterResult.originalToolName,
      });
      const started = await audit.startToolCall({
        toolCallId: event.toolCallId,
        toolName: adapterResult.originalToolName,
        requestedToolName: call.requestedToolName,
        serviceName: adapterResult.serviceName,
        arguments: call.arguments,
      });
      await reportDiagnostic(audit, {
        stage: "audit_start", serviceSlug: adapterResult.serviceSlug, toolName: adapterResult.originalToolName,
      });
      if (adapterResult.errorCode || event.isError) {
        await audit.failToolCall(started.trackedCallId, adapterResult.mcpResult);
        return undefined;
      }
      await audit.settleToolCall(started.trackedCallId, adapterResult.mcpResult);
      await reportDiagnostic(audit, {
        stage: "audit_settle", serviceSlug: adapterResult.serviceSlug, toolName: adapterResult.originalToolName,
      });
    } catch (error) {
      await reportDiagnostic(
        audit,
        failureDiagnostic("audit_settle", error, adapterResult.serviceSlug, adapterResult.originalToolName),
      );
    }
    return undefined;
  });
}

function extractMcpCall(event: ToolCallEvent): TrackedCall | null {
  if (event.toolName !== "mcp") return null;
  const input = event.input as Record<string, unknown>;
  const requestedToolName = typeof input.tool === "string" ? input.tool.trim() : "";
  if (!requestedToolName) return null;
  const args = input.args;
  return {
    requestedToolName,
    arguments: args !== null && typeof args === "object" && !Array.isArray(args) ? args as Record<string, unknown> : {},
    requestedServiceName: typeof input.server === "string" ? input.server : undefined,
  };
}

function parseAdapterResult(
  event: ToolResultEvent,
  call: TrackedCall,
): {
  mcpResult?: unknown;
  originalToolName?: string;
  serviceName?: string;
  serviceSlug?: string;
  errorCode?: string;
} | null {
  const details = event.details;
  if (details === null || typeof details !== "object") return null;
  const record = details as Record<string, unknown>;
  if (record.mode !== "call") return null;
  const serviceName = typeof record.server === "string" ? record.server : call.requestedServiceName;
  const originalToolName = typeof record.tool === "string" ? record.tool : undefined;
  const errorCode = typeof record.error === "string" && /^[a-z0-9_:-]{1,120}$/i.test(record.error)
    ? record.error.toLowerCase()
    : undefined;
  return {
    ...("mcpResult" in record ? { mcpResult: record.mcpResult } : {}),
    ...(originalToolName ? { originalToolName } : {}),
    ...(serviceName ? { serviceName, serviceSlug: SERVICE_SLUGS[serviceName] } : {}),
    ...(errorCode ? { errorCode } : {}),
  };
}

async function reportDiagnostic(audit: AuditClient, diagnostic: PiExtensionDiagnostic): Promise<void> {
  try {
    await audit.recordExtensionDiagnostic(diagnostic);
  } catch {
    // 诊断绝不改变原 MCP 调用的成功/失败语义。
  }
}

function failureDiagnostic(
  stage: PiExtensionDiagnostic["stage"],
  error: unknown,
  serviceSlug?: string,
  toolName?: string,
): PiExtensionDiagnostic {
  const message = error instanceof Error ? error.message : "";
  return {
    stage,
    serviceSlug,
    toolName,
    exceptionType: safeExceptionType(error),
    errorCode: /^[a-z0-9_:-]{1,120}$/i.test(message) ? message.toLowerCase() : "extension_stage_failed",
  };
}

function safeExceptionType(error: unknown): string {
  const name = error instanceof Error ? error.name : "Error";
  return /^[A-Za-z_][A-Za-z0-9_]{0,120}$/.test(name) ? name : "Error";
}

function isHttpUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:";
  } catch {
    return false;
  }
}
