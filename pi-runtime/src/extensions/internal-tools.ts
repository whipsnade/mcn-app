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

const nonEmptyString = Type.String({ minLength: 1 });
const jsonObject = Type.Object({}, { additionalProperties: true });
const evidenceGroups = Type.Record(Type.String(), Type.Array(nonEmptyString));

export const INTERNAL_TOOL_DEFINITIONS = [
  {
    name: "get_session_context",
    description: "读取当前 Run 的受限会话、已发布版本和 Evidence 索引。",
    parameters: Type.Object({}, { additionalProperties: false }),
  },
  {
    name: "search_evidence",
    description: "在当前会话内按关键词、Artifact 或筛选条件检索 Evidence。",
    parameters: Type.Object(
      {
        query: Type.Optional(Type.String({ maxLength: 500 })),
        artifact_id: Type.Optional(nonEmptyString),
        filters: Type.Optional(jsonObject),
        cursor: Type.Optional(Type.String({ maxLength: 200 })),
      },
      { additionalProperties: false },
    ),
  },
  {
    name: "read_tool_result",
    description: "按游标读取指定 Evidence 的原始工具结果分片。",
    parameters: Type.Object(
      {
        evidence_id: nonEmptyString,
        cursor: Type.Optional(Type.Integer({ minimum: 0 })),
        limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 200 })),
      },
      { additionalProperties: false },
    ),
  },
  {
    name: "read_artifact",
    description: "读取当前会话中指定 Artifact 的发布版本或活动 Draft 内容。",
    parameters: Type.Object(
      {
        artifact_id: nonEmptyString,
        version: Type.Optional(Type.Integer({ minimum: 1 })),
        section: Type.Optional(nonEmptyString),
      },
      { additionalProperties: false },
    ),
  },
  {
    name: "build_brand_report_draft",
    description: "用当前会话 Evidence 确定性组装 brand_report_v3 Draft。",
    parameters: Type.Object(
      {
        scope: jsonObject,
        evidence: Type.Optional(evidenceGroups),
        narrative: Type.Optional(jsonObject),
        top_posts_limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 20 })),
      },
      { additionalProperties: false },
    ),
  },
  {
    name: "build_campaign_report_draft",
    description: "用当前会话 Evidence 确定性组装 campaign_report_v2 Draft。",
    parameters: Type.Object(
      {
        scope: jsonObject,
        evidence: Type.Optional(evidenceGroups),
        narrative: Type.Optional(jsonObject),
        top_posts_limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 20 })),
      },
      { additionalProperties: false },
    ),
  },
  {
    name: "build_kol_selection_draft",
    description: "从一个 KOL 列表 Evidence 确定性生成 kol_selection_v3 Draft。",
    parameters: Type.Object(
      {
        scope: jsonObject,
        evidence_id: nonEmptyString,
        narrative: Type.Optional(jsonObject),
      },
      { additionalProperties: false },
    ),
  },
  {
    name: "build_kol_analysis_draft",
    description: "基于已发布圈选名单生成 kol_analysis_v2 Draft。",
    parameters: Type.Object(
      {
        selection_artifact_id: nonEmptyString,
        selection_version: Type.Optional(Type.Integer({ minimum: 1 })),
        analysis_period: Type.Optional(nonEmptyString),
        narrative: Type.Optional(jsonObject),
      },
      { additionalProperties: false },
    ),
  },
  {
    name: "build_kol_detail_draft",
    description: "从达人详情 Evidence 确定性生成 kol_detail_v2 Draft。",
    parameters: Type.Object(
      {
        platform: nonEmptyString,
        kol_uid: nonEmptyString,
        evidence_id: nonEmptyString,
        cache_state: Type.Optional(jsonObject),
        selection_artifact_id: Type.Optional(nonEmptyString),
        selection_version: Type.Optional(nonEmptyString),
      },
      { additionalProperties: false },
    ),
  },
  {
    name: "build_insight_draft",
    description: "基于已发布父版本及引用数据生成 insight_board_v1 Drilldown Draft。",
    parameters: Type.Object(
      {
        parent_artifact_version_id: nonEmptyString,
        question: nonEmptyString,
        title: nonEmptyString,
        scope: Type.Optional(jsonObject),
        blocks: Type.Array(jsonObject, { minItems: 1, maxItems: 50 }),
        narrative: Type.Optional(jsonObject),
      },
      { additionalProperties: false },
    ),
  },
  {
    name: "publish_artifacts",
    description: "发布当前 Run 拥有的一个或多个 Draft，并创建不可变 Artifact Version。",
    parameters: Type.Object(
      { draft_ids: Type.Array(nonEmptyString, { minItems: 1, maxItems: 20 }) },
      { additionalProperties: false },
    ),
  },
  {
    name: "request_clarification",
    description: "对缺失的营销执行条件提出一个受控澄清问题，并可给出二至四个选项。",
    parameters: Type.Object(
      {
        question: Type.String({ minLength: 1, maxLength: 1000 }),
        options: Type.Optional(Type.Array(nonEmptyString, { minItems: 2, maxItems: 4 })),
      },
      { additionalProperties: false },
    ),
  },
] as const;

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
  for (const definition of INTERNAL_TOOL_DEFINITIONS) {
    pi.registerTool({
      name: definition.name,
      label: definition.name,
      description: definition.description,
      parameters: definition.parameters,
      execute: async (_toolCallId, params) => {
        const result = await client.execute(
          definition.name,
          (params ?? {}) as Record<string, unknown>,
        );
        return {
          content: [{ type: "text", text: JSON.stringify(result) }],
          details: {},
        };
      },
    });
  }
}
