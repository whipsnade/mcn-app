import { describe, expect, it } from "vitest";

import { createProductionResourceLoader, createMcpConfig } from "../src/resource-loader.js";
import { resolveMcpAdapterExtensionPath } from "../src/pi-session.js";

const FULL_CATALOG = [
  { service: "insight-cube", adapterName: "mcp__insight_cube", remoteName: "cube_query", schemaDigest: "sha256:a" },
  { service: "social-grow", adapterName: "mcp__social_grow", remoteName: "grow_query", schemaDigest: "sha256:b" },
  { service: "social-grow-content", adapterName: "mcp__social_grow_content", remoteName: "content_query", schemaDigest: "sha256:c" },
  { service: "aktools", adapterName: "mcp__aktools", remoteName: "ak_query", schemaDigest: "sha256:d" },
];

describe("production resource loader", () => {
  it("does not discover skills, context files, or project extensions", async () => {
    const adapterPath = resolveMcpAdapterExtensionPath();
    const loader = createProductionResourceLoader({
      cwd: "/tmp/run-a",
      agentDir: "/tmp/run-a/agent",
      rootPolicy: "ROOT POLICY",
      adapterExtension: adapterPath,
    });
    await loader.reload();
    expect(loader.getSkills().skills).toEqual([]);
    expect(loader.getAgentsFiles().agentsFiles).toEqual([]);
    expect(loader.getPrompts().prompts).toEqual([]);
    expect(loader.getSystemPrompt()).toBe("ROOT POLICY");
    const extensions = loader.getExtensions().extensions;
    expect(extensions.map((item) => item.path)).toEqual([adapterPath]);
  });

  it("writes an adapter-readable MCP config with environment references only", () => {
    const config = createMcpConfig(FULL_CATALOG);
    const serialized = JSON.stringify(config);
    // no literal endpoints or credential values may appear on disk
    expect(serialized).not.toMatch(/https?:\/\//i);
    expect(serialized).not.toMatch(/Bearer [A-Za-z0-9._~+/=-]{4,}/i);
    expect(serialized).not.toMatch(/sk-[A-Za-z0-9]/i);
    expect(config.settings).toEqual({
      hostConfigDiscovery: "off",
      scriptMode: false,
      directTools: false,
      toolPrefix: "none",
      requestTimeoutMs: 180_000,
    });
    expect(config.mcpServers["insight-cube"]).toEqual({
      url: "${PI_DATATAP_URL_INSIGHT_CUBE}",
      headers: { Authorization: "Bearer ${PI_DATATAP_TOKEN}" },
      lifecycle: "eager",
    });
    expect(config.mcpServers["aktools"]).toEqual({
      url: "${PI_DATATAP_URL_AKTOOLS}",
      headers: { Authorization: "Bearer ${PI_DATATAP_TOKEN}" },
      lifecycle: "eager",
    });
  });

  it("serves multiple reviewed tools of one service from a single adapter server", () => {
    const config = createMcpConfig([
      { service: "insight-cube", adapterName: "query_one", remoteName: "remote_one", schemaDigest: "sha256:a" },
      { service: "insight-cube", adapterName: "query_two", remoteName: "remote_two", schemaDigest: "sha256:b" },
      { service: "social-grow", adapterName: "grow", remoteName: "grow", schemaDigest: "sha256:c" },
      { service: "social-grow-content", adapterName: "content", remoteName: "content", schemaDigest: "sha256:d" },
      { service: "aktools", adapterName: "ak", remoteName: "ak", schemaDigest: "sha256:e" },
    ]);
    expect(Object.keys(config.mcpServers)).toEqual([
      "insight-cube",
      "social-grow",
      "social-grow-content",
      "aktools",
    ]);
  });

  it("allows a non-empty approved catalog when a service has no reviewed tools yet", () => {
    const config = createMcpConfig([
      { service: "social-grow", adapterName: "kol_search", remoteName: "kol_search", schemaDigest: "sha256:a" },
    ]);
    expect(Object.keys(config.mcpServers)).toEqual(["social-grow"]);
  });

  it("rejects an empty or unknown-service catalog", () => {
    expect(() => createMcpConfig([])).toThrow("pi_mcp_catalog_incomplete");
    expect(() =>
      createMcpConfig([
        { service: "unknown", adapterName: "x", remoteName: "y", schemaDigest: "sha256:z" },
      ]),
    ).toThrow("pi_mcp_catalog_invalid");
  });
});
