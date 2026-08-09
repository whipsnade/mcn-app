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
    return this.transport.executeInternalTool(toolName, scrubbed);
  }
}
