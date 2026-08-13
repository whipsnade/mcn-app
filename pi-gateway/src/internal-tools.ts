import type { AgentToolResult, ToolDefinition } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

import type { ControlPlaneTransport } from "./control-plane-client.js";

// The direct Pi production surface has one model-owned Artifact Skill entry
// point. Evidence search/read and deterministic Evidence-backed builders stay
// in the current/legacy runtime and are not claimable by this path.
const ALLOWED_INTERNAL_TOOLS = new Set([
  "get_session_context",
  "load_marketing_skill",
  "read_artifact",
  "build_artifact_draft",
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
    // Tool results remain normal model-visible results. Loop warnings are
    // observability only; the model may correct, change tools, or finish text.
    return this.transport.executeInternalTool(toolName, scrubbed);
  }
}

const nonEmptyString = Type.String({ minLength: 1 });
const jsonObject = Type.Object({}, { additionalProperties: true });

/** Reviewed direct-Pi internal tool surface mirrored from FastAPI. */
export const INTERNAL_TOOL_DEFINITIONS = [
  {
    name: "get_session_context",
    description: "读取当前 Run 的受限会话、已发布 Artifact Version 和运行状态。",
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
    name: "read_artifact",
    description: "读取当前 Run 归属的已发布 Artifact Version 或活动 Draft。",
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
    name: "build_artifact_draft",
    description:
      "提交模型自主选择的正式 Artifact Draft。artifact_type 必须属于当前 Run Snapshot 的 " +
      "allowed_artifact_contracts，payload 必须完整符合该类型 Schema；可选 source_tool_call_ids " +
      "只校验属于当前 Run，不读取或重验 MCP 业务结果。",
    parameters: Type.Object(
      {
        artifact_type: Type.String({ minLength: 1, maxLength: 64 }),
        payload: jsonObject,
        source_tool_call_ids: Type.Optional(Type.Array(nonEmptyString, { maxItems: 32 })),
      },
      { additionalProperties: false },
    ),
  },
  {
    name: "publish_artifacts",
    description: "发布当前 Run 拥有的 Draft，并创建严格校验的不可变 Artifact Version。",
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

/** Build SDK tools for exactly the authenticated claim-allowed subset. */
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
