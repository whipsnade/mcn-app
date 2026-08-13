import { describe, expect, it, vi } from "vitest";

import { PiInternalToolsClient } from "../src/internal-tools.js";


describe("production internal tool client", () => {
  it("forwards only tool name and args, never body identity", async () => {
    let request: { toolName: string; args: Record<string, unknown> } | undefined;
    const client = new PiInternalToolsClient({
      executeInternalTool: async (toolName, args) => {
        request = { toolName, args };
        return { status: "success" };
      },
    });
    await expect(client.execute("get_session_context", { user_id: "attacker" })).resolves.toEqual({ status: "success" });
    expect(request).toEqual({ toolName: "get_session_context", args: {} });
  });

  it("scrubs nested identity keys before forwarding arguments", async () => {
    let forwarded: Record<string, unknown> | undefined;
    const client = new PiInternalToolsClient({
      executeInternalTool: async (_toolName, args) => {
        forwarded = args;
        return { status: "success" };
      },
    });
    await client.execute("search_evidence", {
      filters: { session_id: "attacker", query: "brand" },
      items: [{ run_id: "attacker", value: 1 }],
    });
    expect(forwarded).toEqual({ filters: { query: "brand" }, items: [{ value: 1 }] });
  });

  it("turns the durable circuit-open result into a stable business error", async () => {
    const onCircuitOpen = vi.fn();
    const client = new PiInternalToolsClient(
      {
        executeInternalTool: async () => ({
          status: "failed",
          error_type: "agent_loop_circuit_open",
        }),
      },
      { onCircuitOpen },
    );

    await expect(client.execute("build_brand_report_draft", {})).rejects.toMatchObject({
      code: "agent_loop_circuit_open",
    });
    expect(onCircuitOpen).toHaveBeenCalledOnce();
  });
});
