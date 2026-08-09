/**
 * Small adapter-facing MCP billing hook.
 *
 * It intentionally knows no wallet details: the control plane returns a
 * permit or a stable block reason, and the SDK call is made only by the
 * caller after `beforeToolCall` resolves successfully.
 */

export interface McpToolCallInput {
  tool: string;
  server: string;
  args: Record<string, unknown>;
}

export interface McpToolBinding {
  toolName: string;
  server: string;
  remoteName?: string;
}

export interface McpPermit {
  permit_id: string;
  [key: string]: unknown;
}

export interface McpBlocked {
  block: true;
  reason: string;
}

export interface McpFree {
  free: true;
}

export interface McpAccountingControlPlane {
  preflight(input: McpToolCallInput): Promise<McpPermit | McpBlocked>;
  finalize(permit: McpPermit, result: unknown): Promise<unknown>;
  fail(permit: McpPermit, classification: "definitely_not_sent" | "failed_confirmed" | "result_unknown"): Promise<unknown>;
}

const FREE_DISCOVERY_TOOLS = new Set(["connect", "search", "list"]);

export class McpAccountingExtension {
  constructor(private readonly controlPlane: McpAccountingControlPlane) {}

  async beforeToolCall(input: McpToolCallInput): Promise<McpPermit | McpBlocked | McpFree> {
    if (FREE_DISCOVERY_TOOLS.has(input.tool)) return { free: true };
    if (!input.tool || !input.server) return { block: true, reason: "mcp_tool_identity_invalid" };
    try {
      const result = await this.controlPlane.preflight(input);
      if ("block" in result && result.block === true) return result;
      if (!("permit_id" in result) || typeof result.permit_id !== "string" || result.permit_id.length === 0) {
        return { block: true, reason: "mcp_permit_invalid" };
      }
      return result;
    } catch {
      return { block: true, reason: "control_plane_unreachable" };
    }
  }

  async afterToolResult(permit: McpPermit, result: unknown): Promise<unknown> {
    return this.controlPlane.finalize(permit, result);
  }

  async afterToolError(
    permit: McpPermit,
    classification: "definitely_not_sent" | "failed_confirmed" | "result_unknown",
  ): Promise<unknown> {
    return this.controlPlane.fail(permit, classification);
  }
}

/**
 * Install the accounting boundary into Pi's real SDK tool hooks.  The
 * adapter's direct tools and proxy tool both arrive as `tool_call` events;
 * the permit is kept only by tool-call id and is consumed by the matching
 * `tool_result`, so a result can never settle an unrelated call.
 */
export function createMcpAccountingExtensionFactory(
  accounting: McpAccountingExtension,
  bindings: readonly McpToolBinding[] = [],
): ExtensionFactory {
  return (pi) => {
    const permits = new Map<string, McpPermit>();
    const hooks = pi as unknown as {
      on(event: "tool_call", handler: (event: ToolCallEvent) => Promise<unknown>): void;
      on(event: "tool_result", handler: (event: ToolResultEvent) => Promise<unknown>): void;
    };

    hooks.on("tool_call", async (event: ToolCallEvent) => {
      const input = toMcpToolCall(event, bindings);
      if (input === undefined) return;
      const decision = await accounting.beforeToolCall(input);
      if ("block" in decision && decision.block) return decision;
      if ("permit_id" in decision) permits.set(event.toolCallId, decision);
      return undefined;
    });

    hooks.on("tool_result", async (event: ToolResultEvent) => {
      const permit = permits.get(event.toolCallId);
      if (!permit) return;
      permits.delete(event.toolCallId);
      const details = isRecord(event.details) ? event.details : {};
      if (event.isError) {
        const classification = details.classification;
        await accounting.afterToolError(
          permit,
          classification === "definitely_not_sent" ||
            classification === "failed_confirmed" ||
            classification === "result_unknown"
            ? classification
            : "result_unknown",
        );
        return;
      }
      await accounting.afterToolResult(permit, {
        ...details,
        mode: typeof details.mode === "string" ? details.mode : "mcpResult",
      });
    });
  };
}

function toMcpToolCall(
  event: ToolCallEvent,
  bindings: readonly McpToolBinding[],
): McpToolCallInput | undefined {
  const input: Record<string, unknown> = isRecord(event.input)
    ? event.input as Record<string, unknown>
    : {};
  if (event.toolName === "mcp") {
    // Proxy discovery/status/list actions do not invoke a remote tool and are
    // explicitly free.  Only the presence of an explicit `tool` is billable.
    if (typeof input.tool !== "string" || input.tool.length === 0) return undefined;
    const args = normalizeArgs(input.args);
    return {
      tool: input.tool,
      server: typeof input.server === "string" ? input.server : "",
      args,
    };
  }
  const binding = bindings.find((item) => item.toolName === event.toolName);
  if (!binding) return undefined;
  return {
    // The adapter-visible name is the server-owned catalog key used by
    // preflight.  ``remoteName`` is only for the adapter process itself and
    // must never be supplied by the model as an accounting identity.
    tool: binding.toolName,
    server: binding.server,
    args: input,
  };
}

function normalizeArgs(value: unknown): Record<string, unknown> {
  if (value === undefined) return {};
  if (typeof value === "string") {
    try {
      const parsed: unknown = JSON.parse(value);
      return isRecord(parsed) ? parsed : {};
    } catch {
      return {};
    }
  }
  return isRecord(value) ? value : {};
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
import type { ExtensionFactory, ToolCallEvent, ToolResultEvent } from "@earendil-works/pi-coding-agent";
