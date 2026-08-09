import {
  DefaultResourceLoader,
  SettingsManager,
  type ExtensionFactory,
  type ResourceLoader,
} from "@earendil-works/pi-coding-agent";

import type { AdapterCatalogEntry, SkillCatalogEntry } from "./protocol.js";

const REQUIRED_SERVICES = ["insight-cube", "social-grow", "social-grow-content", "aktools"] as const;

export interface ProductionResourceLoaderOptions {
  cwd: string;
  agentDir: string;
  rootPolicy: string;
  skillCatalog?: readonly SkillCatalogEntry[];
  adapterCatalog?: readonly AdapterCatalogEntry[];
  adapterExtension?: string;
  auditExtension?: string;
  extensionFactories?: readonly ExtensionFactory[];
}

/**
 * Resource boundary for a single production Run.  All discovery switches are
 * disabled; only the two explicitly supplied production extension slots are loaded.
 */
export function createProductionResourceLoader(options: ProductionResourceLoaderOptions): ResourceLoader {
  const factories = options.extensionFactories ? [...options.extensionFactories] : [noopExtension, noopExtension];
  const loader = new DefaultResourceLoader({
    cwd: options.cwd,
    agentDir: options.agentDir,
    settingsManager: SettingsManager.inMemory(),
    extensionFactories: factories,
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
      `- ${skill.name}: ${skill.description} (version=${skill.version}, artifact_contract=${skill.artifactContract})`,
    ),
  ].join("\n");
}

export function createMcpConfig(catalog: readonly AdapterCatalogEntry[]): {
  mcpServers: Record<string, { command: string; args: string[]; env: Record<string, string> }>;
} {
  const seen = new Set<string>();
  const mcpServers: Record<string, { command: string; args: string[]; env: Record<string, string> }> = {};
  for (const entry of catalog) {
    if (!REQUIRED_SERVICES.includes(entry.service as (typeof REQUIRED_SERVICES)[number]) || seen.has(entry.service)) {
      throw new Error("pi_mcp_catalog_invalid");
    }
    if (!entry.adapterName || !entry.remoteName || !/^sha256:[a-f0-9]{1,128}$/i.test(entry.schemaDigest)) {
      throw new Error("pi_mcp_catalog_invalid");
    }
    seen.add(entry.service);
    mcpServers[entry.service] = {
      command: "pi-mcp-adapter",
      args: ["--service", entry.service, "--remote-tool", entry.remoteName],
      env: {
        PI_MCP_RUN_ID: "${PI_RUN_ID}",
        PI_MCP_SERVICE: entry.service,
        PI_MCP_ADAPTER_NAME: entry.adapterName,
      },
    };
  }
  if (seen.size !== REQUIRED_SERVICES.length) {
    throw new Error("pi_mcp_catalog_incomplete");
  }
  return { mcpServers };
}

async function noopExtension(): Promise<void> {
  // The actual adapter and audit factories are injected by the authenticated
  // control-plane task.  Keeping these slots explicit prevents auto-discovery.
}
