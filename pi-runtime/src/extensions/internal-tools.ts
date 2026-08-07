/**
 * 受控内部工具桥（Task 5）。
 *
 * 只把设计白名单内的历史读取、六类 Builder、get_session_context 与
 * publish_artifacts 暴露给 Pi；bash/shell/文件编辑/任意 HTTP/Draft 直写/
 * 计算/记忆等一律拒绝。DataTap 由 Task 4 Extension 直连，本桥不暴露 MCP。
 * token 由底层 HTTP client 放在 Authorization 头，body 绝不含任何身份键。
 */

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
