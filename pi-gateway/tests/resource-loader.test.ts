import { describe, expect, it } from "vitest";

import { createProductionResourceLoader, createMcpConfig } from "../src/resource-loader.js";

describe("production resource loader", () => {
  it("does not discover skills, context files, or project extensions", async () => {
    const loader = createProductionResourceLoader({
      cwd: "/tmp/run-a",
      agentDir: "/tmp/run-a/agent",
      rootPolicy: "ROOT POLICY",
      adapterExtension: "production-adapter",
      auditExtension: "production-audit",
    });
    await loader.reload();
    expect(loader.getSkills().skills).toEqual([]);
    expect(loader.getAgentsFiles().agentsFiles).toEqual([]);
    expect(loader.getPrompts().prompts).toEqual([]);
    expect(loader.getSystemPrompt()).toBe("ROOT POLICY");
    expect(loader.getExtensions().extensions).toHaveLength(2);
    expect(loader.getExtensions().extensions.every((item) => item.path.startsWith("<inline:"))).toBe(true);
  });

  it("writes an endpoint-free MCP config from the audited catalog", () => {
    const config = createMcpConfig([
      { service: "insight-cube", adapterName: "mcp__insight_cube", remoteName: "cube_query", schemaDigest: "sha256:a" },
      { service: "social-grow", adapterName: "mcp__social_grow", remoteName: "grow_query", schemaDigest: "sha256:b" },
      { service: "social-grow-content", adapterName: "mcp__social_grow_content", remoteName: "content_query", schemaDigest: "sha256:c" },
      { service: "aktools", adapterName: "mcp__aktools", remoteName: "ak_query", schemaDigest: "sha256:d" },
    ]);
    expect(JSON.stringify(config)).not.toMatch(/https?:\/\//i);
    expect(JSON.stringify(config)).not.toMatch(/token|api[_-]?key|authorization/i);
    expect(config.mcpServers).toHaveProperty("insight-cube");
    expect(config.mcpServers).toHaveProperty("aktools");
  });

  it("keeps multiple reviewed tools in one service without overwriting a server", () => {
    const config = createMcpConfig([
      { service: "insight-cube", adapterName: "query_one", remoteName: "remote_one", schemaDigest: "sha256:a" },
      { service: "insight-cube", adapterName: "query_two", remoteName: "remote_two", schemaDigest: "sha256:b" },
      { service: "social-grow", adapterName: "grow", remoteName: "grow", schemaDigest: "sha256:c" },
      { service: "social-grow-content", adapterName: "content", remoteName: "content", schemaDigest: "sha256:d" },
      { service: "aktools", adapterName: "ak", remoteName: "ak", schemaDigest: "sha256:e" },
    ]);
    expect(Object.keys(config.mcpServers)).toEqual([
      "insight-cube",
      "insight-cube__query_two",
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
});
