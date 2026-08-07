/**
 * 受控内部工具桥（Task 5）。
 *
 * 只把设计白名单内的历史读取、六类 Builder、get_session_context 与
 * publish_artifacts 暴露给 Pi；bash/shell/文件编辑/任意 HTTP/Draft 直写/
 * 计算/记忆等一律拒绝。DataTap 由 Task 4 Extension 直连，本桥不暴露 MCP。
 * token 由底层 HTTP client 放在 Authorization 头，body 绝不含任何身份键。
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

import type { PiPocHttpClient } from "../http/client.js";

export const PI_INTERNAL_TOOL_NAMES = [
  "get_session_context",
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
] as const;

export type PiInternalToolName = (typeof PI_INTERNAL_TOOL_NAMES)[number];

export function isAllowedInternalTool(name: string): name is PiInternalToolName {
  return (PI_INTERNAL_TOOL_NAMES as readonly string[]).includes(name);
}

export class PiInternalToolsClient {
  constructor(private readonly http: PiPocHttpClient) {}

  async execute(toolName: string, args: Record<string, unknown>): Promise<unknown> {
    if (!isAllowedInternalTool(toolName)) {
      throw new Error(`pi_internal_tool_not_allowed:${toolName}`);
    }
    return this.http.executeInternalTool(toolName, args);
  }
}

/**
 * 将受控后端工具显式注册给本 Run 的 Pi Extension。
 *
 * 参数校验、身份绑定与发布租约仍在 FastAPI 内完成；这里不添加任何工具、参数或
 * 返回值改写逻辑，只把 Pi 输入原样交给经过白名单校验的 HTTP client。
 */
export function registerInternalTools(pi: ExtensionAPI, client: PiInternalToolsClient): void {
  for (const name of PI_INTERNAL_TOOL_NAMES) {
    pi.registerTool({
      name,
      label: name,
      description: "受控 KOL Insight POC 内部工具。",
      parameters: Type.Object({}, { additionalProperties: Type.Unknown() }),
      execute: async (_toolCallId, params) => {
        const result = await client.execute(name, (params ?? {}) as Record<string, unknown>);
        return {
          content: [{ type: "text", text: JSON.stringify(result) }],
          details: {},
        };
      },
    });
  }
}
