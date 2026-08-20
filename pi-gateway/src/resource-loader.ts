import {
  DefaultResourceLoader,
  SettingsManager,
  type ExtensionFactory,
  type ResourceLoader,
} from "@earendil-works/pi-coding-agent";
import { dirname, isAbsolute, resolve } from "node:path";

import type { AdapterCatalogEntry, SkillCatalogEntry } from "./protocol.js";

const ALLOWED_SERVICES = ["insight-cube", "social-grow", "social-grow-content", "aktools"] as const;

export interface ProductionResourceLoaderOptions {
  cwd: string;
  agentDir: string;
  rootPolicy: string;
  skillCatalog?: readonly SkillCatalogEntry[];
  /** Only the current Run's atomically materialized Skill directory. */
  additionalSkillPaths?: readonly string[];
  adapterCatalog?: readonly AdapterCatalogEntry[];
  /** Absolute path of the reviewed pi-mcp-adapter entrypoint (jiti-loaded). */
  adapterExtension?: string;
  extensionFactories?: readonly ExtensionFactory[];
}

/**
 * Resource boundary for a single production Run.  All discovery switches are
 * disabled; only the explicit adapter path and audited factory slots load.
 */
export function createProductionResourceLoader(options: ProductionResourceLoaderOptions): ResourceLoader {
  const additionalSkillPaths = [...(options.additionalSkillPaths ?? [])];
  const runRoot = resolve(options.cwd);
  for (const path of additionalSkillPaths) {
    const resolvedPath = resolve(path);
    if (!isAbsolute(path) || dirname(resolvedPath) !== runRoot) {
      throw new Error("pi_skill_snapshot_path_invalid");
    }
  }
  const loader = new DefaultResourceLoader({
    cwd: options.cwd,
    agentDir: options.agentDir,
    settingsManager: SettingsManager.inMemory(),
    additionalExtensionPaths: options.adapterExtension ? [options.adapterExtension] : [],
    additionalSkillPaths,
    extensionFactories: options.extensionFactories ? [...options.extensionFactories] : [],
    noExtensions: true,
    noSkills: true,
    noPromptTemplates: true,
    noThemes: true,
    noContextFiles: true,
    systemPrompt: options.rootPolicy,
    appendSystemPrompt: [formatSkillDirectory(options.skillCatalog ?? [])],
  });
  return loader;
}

export function formatSkillDirectory(skills: readonly SkillCatalogEntry[]): string {
  return [
    "当前 Run 可用专项 Skill 目录（只列目录，不含正文）：",
    ...skills.map((skill) =>
      `- ${skill.name}: ${skill.description} (version=${skill.version}, artifact_contract=${skill.artifactContract ?? "none"})`,
    ),
  ].join("\n");
}

export interface ProductionMcpConfig {
  settings: {
    hostConfigDiscovery: "off";
    scriptMode: false;
    directTools: false;
    toolPrefix: "none";
    requestTimeoutMs: number;
    /** Trusted internal MCP owns pagination; adapter must not summarize/spill results. */
    outputGuard: false;
  };
  mcpServers: Record<
    string,
    { url: string; headers: { Authorization: string }; lifecycle: "lazy" | "eager" }
  >;
}

/**
 * Render the adapter-readable `.mcp.json` for a Run.  The file only carries
 * environment *references*; the decrypted endpoint/token values exist solely
 * in the child process environment built by ``secret-env.ts``.
 */
export function createMcpConfig(
  catalog: readonly AdapterCatalogEntry[],
  options: { lifecycle?: "lazy" | "eager" } = {},
): ProductionMcpConfig {
  const mcpServers: ProductionMcpConfig["mcpServers"] = {};
  for (const entry of catalog) {
    if (!ALLOWED_SERVICES.includes(entry.service as (typeof ALLOWED_SERVICES)[number])) {
      throw new Error("pi_mcp_catalog_invalid");
    }
    if (
      !entry.adapterName || !/^[A-Za-z0-9._:-]{1,128}$/.test(entry.adapterName) ||
      !entry.remoteName || !/^[A-Za-z0-9._:-]{1,128}$/.test(entry.remoteName) ||
      !/^sha256:[a-f0-9]{1,128}$/i.test(entry.schemaDigest)
    ) {
      throw new Error("pi_mcp_catalog_invalid");
    }
    if (mcpServers[entry.service]) continue;
    const envName = `PI_DATATAP_URL_${entry.service.replace(/[^a-z0-9]+/gi, "_").toUpperCase()}`;
    mcpServers[entry.service] = {
      url: `\${${envName}}`,
      headers: { Authorization: "Bearer ${PI_DATATAP_TOKEN}" },
      lifecycle: options.lifecycle ?? "eager",
    };
  }
  if (Object.keys(mcpServers).length === 0) {
    throw new Error("pi_mcp_catalog_incomplete");
  }
  return {
    settings: {
      hostConfigDiscovery: "off",
      scriptMode: false,
      directTools: false,
      // 模型可见名 = 裸 remote 名（与 claim catalog 投影的 adapter_visible_name
      // 一致）。真实模型经通用 mcp 代理即以裸名寻址；prefixed 旧名仍由计费
      // 扩展映射到已审核身份，但 adapter 分发只接受裸名（未知名 fail-closed
      // tool_not_found → definitely_not_sent 释放，绝不计费）。
      toolPrefix: "none",
      requestTimeoutMs: 180_000,
      // The internal MCP contract owns pagination and response bounds.  The
      // adapter's optional guard would truncate/spill a successful result and
      // replace it with a success preview, which is not transparent delivery.
      outputGuard: false,
    },
    mcpServers,
  };
}
