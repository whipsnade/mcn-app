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
});
