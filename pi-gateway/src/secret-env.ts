import type { SecretBundle } from "./protocol.js";

const SECRET_KEYS = new Set([
  "PI_MODEL_BASE_URL",
  "PI_MODEL_API_KEY",
  "PI_DATATAP_TOKEN",
  "PI_DATATAP_URL_INSIGHT_CUBE",
  "PI_DATATAP_URL_SOCIAL_GROW",
  "PI_DATATAP_URL_SOCIAL_GROW_CONTENT",
  "PI_DATATAP_URL_AKTOOLS",
]);

const SAFE_PARENT_KEYS = new Set(["PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR"]);

/** Build only the child-process environment; never mutate process.env. */
export function buildSecretEnv(
  secrets: SecretBundle,
  parent: NodeJS.ProcessEnv = process.env,
  runId?: string,
): Record<string, string> {
  const child: Record<string, string> = {};
  for (const [key, value] of Object.entries(parent)) {
    if (value !== undefined && SAFE_PARENT_KEYS.has(key)) {
      child[key] = value;
    }
  }
  if (runId) child.PI_RUN_ID = runId;
  child.PI_MODEL_BASE_URL = secrets.modelBaseUrl;
  child.PI_MODEL_API_KEY = secrets.modelApiKey;
  child.PI_DATATAP_TOKEN = secrets.datatapToken;
  for (const [name, url] of Object.entries(secrets.datatapUrls)) {
    const normalized = name.replace(/[^a-z0-9]+/gi, "_").toUpperCase();
    child[`PI_DATATAP_URL_${normalized}`] = url;
  }
  return child;
}

/** Remove all secret values from a child env object after the worker exits. */
export function clearSecretEnv(env: Record<string, string>): void {
  for (const key of Object.keys(env)) {
    if (SECRET_KEYS.has(key) || key.startsWith("PI_DATATAP_URL_") || key === "PI_RUN_ID") {
      delete env[key];
    }
  }
}
