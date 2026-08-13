import type { AgentToolResult, ToolDefinition } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

import type { ControlPlaneTransport } from "./control-plane-client.js";

const ALLOWED_INTERNAL_TOOLS = new Set([
  "get_session_context",
  "load_marketing_skill",
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
]);

const RESERVED_KEYS = new Set(["user_id", "session_id", "run_id", "attempt_id", "tenant_id"]);

function scrubReservedKeys(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(scrubReservedKeys);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>)
      .filter(([key]) => !RESERVED_KEYS.has(key.toLowerCase()))
      .map(([key, item]) => [key, scrubReservedKeys(item)]),
  );
}

export class PiInternalToolsClient {
  constructor(private readonly transport: ControlPlaneTransport) {}

  async execute(toolName: string, args: Record<string, unknown>): Promise<unknown> {
    if (!ALLOWED_INTERNAL_TOOLS.has(toolName)) throw new Error("pi_internal_tool_not_allowed");
    const scrubbed = scrubReservedKeys(args) as Record<string, unknown>;
    // A durable loop warning is returned as the original tool result. It is
    // observability only; the model remains free to continue, retry, change
    // tools or finish with text. Generic max_decisions is the platform fuse.
    return this.transport.executeInternalTool(toolName, scrubbed);
  }
}

const nonEmptyString = Type.String({ minLength: 1 });
const jsonObject = Type.Object({}, { additionalProperties: true });
const evidenceGroups = Type.Record(Type.String(), Type.Array(nonEmptyString));

/**
 * Reviewed internal tool surface mirrored from the FastAPI production bridge.
 * The server re-validates identity, schema and lease on every call; these
 * definitions only describe the wire contract to the model.
 */
export const INTERNAL_TOOL_DEFINITIONS = [
  {
    name: "get_session_context",
    description: "读取当前 Run 的受限会话、已发布版本和 Evidence 索引。",
    parameters: Type.Object({}, { additionalProperties: false }),
  },
  {
    name: "load_marketing_skill",
    description: "从当前 Run 的营销能力包快照加载一个已启用专项 Skill 正文。",
    parameters: Type.Object(
      {
        skill_name: Type.String({ minLength: 1, maxLength: 64, pattern: "^[a-z0-9][a-z0-9-]{0,63}$" }),
        requested_version: Type.Optional(Type.String({ minLength: 1, maxLength: 64 })),
      },
      { additionalProperties: false },
    ),
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

export function isAllowedInternalTool(name: string): boolean {
  return ALLOWED_INTERNAL_TOOLS.has(name);
}

/**
 * Build SDK custom tools for exactly the claim-allowed subset.  Every
 * execution crosses the parent IPC bridge; the child holds no HMAC secret,
 * lease token or service credential for this path.
 */
export function buildInternalToolDefinitions(
  client: PiInternalToolsClient,
  allowedNames?: readonly string[],
): ToolDefinition[] {
  const allowed = allowedNames === undefined ? new Set<string>() : new Set(allowedNames);
  return INTERNAL_TOOL_DEFINITIONS.filter(
    (definition) => allowed.has(definition.name) && isAllowedInternalTool(definition.name),
  ).map((definition) => ({
    name: definition.name,
    label: definition.name,
    description: definition.description,
    parameters: definition.parameters,
    execute: async (_toolCallId, params): Promise<AgentToolResult<unknown>> => {
      const result = await client.execute(
        definition.name,
        (params ?? {}) as Record<string, unknown>,
      );
      return {
        content: [{ type: "text", text: JSON.stringify(result) }],
        details: {},
      };
    },
  })) as ToolDefinition[];
}
