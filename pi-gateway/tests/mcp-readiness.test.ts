import { describe, expect, it } from "vitest";

import { createMcpReadinessGate } from "../src/mcp-readiness.js";

describe("MCP readiness gate", () => {
  it("waits for every required server to be connected with tool metadata", async () => {
    const gate = createMcpReadinessGate(["insight-cube", "social-grow"], 100);
    const waiting = gate.waitUntilReady();
    gate.observeSnapshot({
      version: 1,
      servers: [
        { name: "insight-cube", status: "connected", toolCount: 1, disabled: false },
        { name: "social-grow", status: "not-connected", toolCount: 1, disabled: false },
      ],
      totalTools: 2,
      totalResources: 0,
      connectedCount: 1,
      disabledCount: 0,
    });
    let settled = false;
    void waiting.then(() => { settled = true; });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(settled).toBe(false);
    gate.observeSnapshot({
      version: 1,
      servers: [
        { name: "insight-cube", status: "connected", toolCount: 1, disabled: false },
        { name: "social-grow", status: "connected", toolCount: 2, disabled: false },
      ],
      totalTools: 3,
      totalResources: 0,
      connectedCount: 2,
      disabledCount: 0,
    });
    await expect(waiting).resolves.toBeUndefined();
    gate.dispose();
  });

  it("fails with a stable startup code when a required server fails", async () => {
    const gate = createMcpReadinessGate(["insight-cube"], 100);
    const waiting = gate.waitUntilReady();
    gate.observeSnapshot({
      version: 1,
      servers: [{ name: "insight-cube", status: "failed", toolCount: 0, disabled: false }],
      totalTools: 0,
      totalResources: 0,
      connectedCount: 0,
      disabledCount: 0,
    });
    await expect(waiting).rejects.toThrow("pi_mcp_readiness_failed");
    gate.dispose();
  });

  it("does not reuse a stale eager load snapshot after session_start restarts the adapter", async () => {
    const gate = createMcpReadinessGate(["insight-cube"], 100);
    gate.observeSnapshot({
      version: 1,
      servers: [{ name: "insight-cube", status: "connected", toolCount: 1, disabled: false }],
      totalTools: 1,
      totalResources: 0,
      connectedCount: 1,
      disabledCount: 0,
    });
    gate.beginSession();
    let settled = false;
    const waiting = gate.waitUntilReady().then(() => { settled = true; });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(settled).toBe(false);
    gate.observeSnapshot({
      version: 1,
      servers: [{ name: "insight-cube", status: "connected", toolCount: 2, disabled: false }],
      totalTools: 2,
      totalResources: 0,
      connectedCount: 1,
      disabledCount: 0,
    });
    await waiting;
    gate.dispose();
  });
});
