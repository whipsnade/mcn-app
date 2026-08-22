import { describe, expect, it, vi } from "vitest";

import {
  normalizePiGatewayAdapterCatalog,
  parsePiGatewayClaimResponse,
  PI_GATEWAY_ADAPTER_CATALOG_MAX_BYTES,
  PI_GATEWAY_ADAPTER_CATALOG_MAX_ENTRIES,
} from "../src/protocol.js";
import { createMcpAccountingExtensionFactory, McpAccountingExtension } from "../src/mcp-accounting-extension.js";
import { createMcpConfig } from "../src/resource-loader.js";

function wireCatalog(count: number) {
  const services = ["insight-cube-mcp", "social-grow-mcp", "social-grow-content-mcp", "bilibili-mcp"];
  return Array.from({ length: count }, (_, index) => ({
    catalog_entry_id: `catalog-${index}`,
    adapter_visible_name: `adapter_${index}`,
    service: services[index % services.length],
    remote_name: `remote_${index}`,
    input_schema_digest: `sha256:${index.toString(16).padStart(64, "0")}`,
  }));
}

function normalizedCatalog(count: number) {
  const services = ["insight-cube", "social-grow", "social-grow-content", "aktools"];
  return Array.from({ length: count }, (_, index) => ({
    service: services[index % services.length],
    adapterName: `adapter_${index}`,
    remoteName: `remote_${index}`,
    schemaDigest: `sha256:${index.toString(16).padStart(64, "0")}`,
  }));
}

function claim(adapter_catalog: unknown[]) {
  return {
    run_id: "run-1",
    attempt_id: "attempt-1",
    lease_token: "lease-token-that-is-long-enough-1234567890",
    lease_expires_at: 1_800_000_000,
    runtime_snapshot: { config_version_id: "cfg-1" },
    transcript: [],
    secret_envelope: { alg: "AES-256-GCM", nonce: "A".repeat(16), ciphertext: "B".repeat(16) },
    adapter_catalog,
    internal_tools: [],
  };
}

describe("bounded complete adapter catalog", () => {
  it("accepts 58 and the exact 128-entry boundary without dropping entries", () => {
    expect(PI_GATEWAY_ADAPTER_CATALOG_MAX_ENTRIES).toBe(128);
    for (const count of [58, 128]) {
      const parsed = parsePiGatewayClaimResponse(claim(wireCatalog(count)));
      expect(parsed.adapter_catalog).toHaveLength(count);
      expect(normalizePiGatewayAdapterCatalog(parsed.adapter_catalog)).toHaveLength(count);
    }
  });

  it("rejects 129 entries and canonical JSON above 128 KiB", () => {
    expect(() => parsePiGatewayClaimResponse(claim(wireCatalog(129)))).toThrow(
      "pi_gateway_claim_response_invalid",
    );
    const oversized = wireCatalog(1);
    oversized[0].remote_name = "x".repeat(PI_GATEWAY_ADAPTER_CATALOG_MAX_BYTES);
    expect(() => parsePiGatewayClaimResponse(claim(oversized))).toThrow(
      "pi_gateway_claim_response_invalid",
    );
  });

  it("fails closed on duplicate normalized identities and preserves first/last entries", () => {
    const catalog = wireCatalog(58);
    const normalized = normalizePiGatewayAdapterCatalog(catalog);
    expect(normalized[0].adapterName).toBe("adapter_0");
    expect(normalized[57].adapterName).toBe("adapter_57");
    const duplicate = wireCatalog(2);
    duplicate[1].adapter_visible_name = duplicate[0].adapter_visible_name;
    duplicate[0].service = "aktools-mcp";
    duplicate[1].service = "bilibili-mcp";
    expect(() => normalizePiGatewayAdapterCatalog(duplicate)).toThrow(
      "pi_gateway_adapter_catalog_invalid",
    );
  });

  it("keeps 58 catalog entries behind four MCP servers and preserves safe settings", () => {
    const config = createMcpConfig(normalizedCatalog(58));
    expect(Object.keys(config.mcpServers)).toEqual([
      "insight-cube",
      "social-grow",
      "social-grow-content",
      "aktools",
    ]);
    expect(config.settings.directTools).toBe(false);
    expect(config.settings.scriptMode).toBe(false);
    expect(config.settings.outputGuard).toBe(false);
    expect("tools" in config).toBe(false);
  });

  it("resolves first and last accounting bindings and blocks an unbound tool", async () => {
    const catalog = normalizedCatalog(58);
    const serverFor = (service: string) => `${service}-mcp`;
    const bindings = catalog.map((entry) => ({
      toolName: entry.adapterName,
      server: serverFor(entry.service),
      remoteName: entry.remoteName,
    }));
    const preflight = vi.fn(async (input: { tool: string; server: string }) => ({
      permit_id: `permit-${input.tool}`,
    }));
    const extension = new McpAccountingExtension({
      preflight,
      finalize: vi.fn(),
      fail: vi.fn(),
    });
    const handlers = new Map<string, (event: any) => Promise<unknown>>();
    createMcpAccountingExtensionFactory(extension, bindings)({
      on: (name: string, handler: (event: any) => Promise<unknown>) => handlers.set(name, handler),
    } as any);

    await handlers.get("tool_call")?.({
      type: "tool_call", toolName: "mcp", toolCallId: "first", input: { tool: catalog[0].adapterName, args: {} },
    });
    await handlers.get("tool_call")?.({
      type: "tool_call", toolName: "mcp", toolCallId: "last", input: { tool: catalog[57].adapterName, args: {} },
    });
    const blocked = await handlers.get("tool_call")?.({
      type: "tool_call", toolName: "mcp", toolCallId: "unknown", input: { tool: "not-bound", args: {} },
    });

    expect(preflight).toHaveBeenNthCalledWith(1, expect.objectContaining({ tool: "adapter_0" }));
    expect(preflight).toHaveBeenNthCalledWith(2, expect.objectContaining({ tool: "adapter_57" }));
    expect(preflight).toHaveBeenCalledTimes(2);
    expect(blocked).toEqual({ block: true, reason: "mcp_tool_identity_invalid" });
  });
});
