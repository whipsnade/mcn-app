/** Task 8D 单工具真实冒烟：以 pi-mcp-adapter + 项目 .mcp.json 走完整代理链路。 */

import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

import { createMcpAdapter } from "pi-mcp-adapter";

import { installPocAuditExtension, readRuntimeConfigFromEnv } from "../src/extensions/poc-runtime.js";
import { PiPocHttpClient, type PiExtensionStage } from "../src/http/client.js";

const SERVICE = "social-grow-content";
const TOOL = "hotwords_xiaohongshu_dictionary";
type SafeStage = PiExtensionStage;
type Handler = (event: any, context?: any) => Promise<unknown> | unknown;

type SmokeHost = {
  handlers: Map<string, Handler[]>;
  tools: Map<string, any>;
  on: (name: string, handler: Handler) => void;
  registerTool: (tool: any) => void;
  registerCommand: () => void;
  registerFlag: () => void;
  getFlag: () => undefined;
  getAllTools: () => any[];
  getActiveTools: () => string[];
  setActiveTools: () => void;
  events: { emit: () => boolean };
};

function safeError(stage: SafeStage, error: unknown): void {
  const message = error instanceof Error ? error.message : "pi_poc_adapter_smoke_failed";
  const code = /^[a-z0-9_:-]{1,120}$/i.test(message) ? message.toLowerCase() : "pi_poc_adapter_smoke_failed";
  console.error(JSON.stringify({ code, stage, service_slug: SERVICE, tool_name: TOOL, exception_type: error instanceof Error ? error.name : "Error" }));
}

function createHost(): SmokeHost {
  const handlers = new Map<string, Handler[]>();
  const tools = new Map<string, any>();
  return {
    handlers,
    tools,
    on: (name, handler) => handlers.set(name, [...(handlers.get(name) ?? []), handler]),
    registerTool: (tool) => tools.set(tool.name, tool),
    registerCommand: () => undefined,
    registerFlag: () => undefined,
    getFlag: () => undefined,
    getAllTools: () => [...tools.values()],
    getActiveTools: () => [...tools.keys()],
    setActiveTools: () => undefined,
    events: { emit: () => true },
  };
}

async function emit(host: SmokeHost, name: string, event: any, context?: any): Promise<unknown[]> {
  const results: unknown[] = [];
  for (const handler of host.handlers.get(name) ?? []) results.push(await handler(event, context));
  return results;
}

async function main(): Promise<void> {
  if (process.env.RUN_PI_POC_DATATAP_SMOKE !== "1") throw new Error("pi_poc_smoke_opt_in_required");
  if (!process.env.PI_RUNTIME_POC_RUN_ID?.trim()) throw new Error("pi_poc_smoke_run_id_required");
  if (!process.env.PI_RUNTIME_POC_BASE_URL?.trim()) throw new Error("pi_poc_smoke_audit_url_required");
  if (!process.env.DATATAP_MCP_TOKEN?.trim()) throw new Error("pi_poc_smoke_datatap_token_required");
  const config = readRuntimeConfigFromEnv();
  const audit = new PiPocHttpClient({ baseUrl: config.baseUrl, runId: config.runId, token: config.runToken });
  const agentDir = await mkdtemp(join(tmpdir(), "pi-datatap-smoke-"));
  process.env.PI_CODING_AGENT_DIR = agentDir;
  await writeFile(join(agentDir, "mcp-cache.json"), '{"version":1,"servers":{}}');
  const host = createHost();
  try {
    createMcpAdapter({ configPath: resolve(".mcp.json") })(host as never);
    installPocAuditExtension(host as never, audit);

    await audit.recordExtensionDiagnostic({ stage: "config", serviceSlug: "social-grow-content-mcp", toolName: TOOL });
    await emit(host, "session_start", {}, {
      mode: "print", hasUI: false, cwd: process.cwd(), signal: undefined, ui: undefined,
    });
    const proxy = host.tools.get("mcp");
    if (!proxy) throw new Error("pi_poc_adapter_proxy_missing");
    if (host.tools.has("mcpScript")) throw new Error("pi_poc_adapter_script_mode_enabled");
    const connectResult = await proxy.execute(
      "single-datatap-smoke-connect",
      { connect: SERVICE },
      undefined,
      undefined,
      { mode: "print", hasUI: false, cwd: process.cwd(), signal: undefined },
    );
    const visibleTool = visibleToolName(connectResult.details, TOOL);
    if (!visibleTool) throw new Error("pi_poc_smoke_tool_not_visible");
    await audit.recordExtensionDiagnostic({ stage: "connect", serviceSlug: "social-grow-content-mcp", toolName: TOOL });
    await audit.recordExtensionDiagnostic({ stage: "tools_list", serviceSlug: "social-grow-content-mcp", toolName: TOOL });
    await audit.recordExtensionDiagnostic({ stage: "schema_validate", serviceSlug: "social-grow-content-mcp", toolName: TOOL });

    const input = { tool: visibleTool, args: {}, server: SERVICE };
    const toolCallId = "single-datatap-smoke-call-1";
    await emit(host, "tool_call", { type: "tool_call", toolCallId, toolName: "mcp", input });
    const result = await proxy.execute(toolCallId, input, undefined, undefined, {
      mode: "print", hasUI: false, cwd: process.cwd(), signal: undefined,
    });
    await emit(host, "tool_result", {
      type: "tool_result", toolCallId, toolName: "mcp", input,
      content: result.content, isError: result.details?.error !== undefined, details: result.details,
    });
    if (result.details?.error !== undefined) throw new Error("pi_poc_smoke_tool_error");
    await audit.completeSingleToolSmoke();
  } catch (error) {
    try {
      await audit.failSingleToolSmoke(safeErrorCode(error));
    } catch {
      // 终态收口失败不能扩展诊断信息或改变原错误分类。
    }
    throw error;
  } finally {
    await emit(host, "session_shutdown", {});
    await rm(agentDir, { recursive: true, force: true });
  }
}

function visibleToolName(details: unknown, originalToolName: string): string | undefined {
  if (details === null || typeof details !== "object") return undefined;
  const tools = (details as { tools?: unknown }).tools;
  if (!Array.isArray(tools)) return undefined;
  return tools.find((tool): tool is string =>
    typeof tool === "string" && (tool === originalToolName || tool.endsWith(`_${originalToolName}`)),
  );
}

function safeErrorCode(error: unknown): string {
  const message = error instanceof Error ? error.message : "pi_poc_adapter_smoke_failed";
  return /^[a-z0-9_:-]{1,120}$/i.test(message) ? message.toLowerCase() : "pi_poc_adapter_smoke_failed";
}

void main().catch((error) => {
  safeError("mcp_call", error);
  process.exitCode = 1;
});
