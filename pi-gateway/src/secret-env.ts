import { createDecipheriv, hkdfSync } from "node:crypto";

import type { RuntimeSecretEnvelope } from "./protocol.js";
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

const SAFE_PARENT_KEYS = new Set(["PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ"]);

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

/**
 * Best-effort erasure of a decrypted bundle once the child process has been
 * spawned with its own environment copy.  JavaScript strings are immutable,
 * so this overwrites the reachable references and the caller drops the rest.
 */
export function clearSecretBundle(bundle: SecretBundle): void {
  bundle.modelBaseUrl = "";
  bundle.modelApiKey = "";
  bundle.datatapToken = "";
  const urls = bundle.datatapUrls as Record<string, string>;
  for (const key of Object.keys(urls)) {
    urls[key] = "";
  }
}

export async function decryptSecretEnvelope(
  envelope: RuntimeSecretEnvelope,
  leaseToken: string,
  binding: { runId: string; attemptId: string; configVersionId: string; gatewayId: string },
): Promise<SecretBundle> {
  try {
    if (envelope.alg !== "AES-256-GCM") throw new Error("alg");
    const aad = Buffer.from(`${binding.runId}:${binding.attemptId}:${binding.configVersionId}:${binding.gatewayId}`);
    const key = Buffer.from(hkdfSync("sha256", Buffer.from(leaseToken), Buffer.from("pi-gateway-secret-v1"), Buffer.from(`lease:${aad.toString()}`), 32));
    const ciphertext = Buffer.from(envelope.ciphertext, "base64");
    if (ciphertext.length <= 16) throw new Error("ciphertext");
    const decipher = createDecipheriv("aes-256-gcm", key, Buffer.from(envelope.nonce, "base64"));
    decipher.setAAD(aad);
    decipher.setAuthTag(ciphertext.subarray(-16));
    const plaintext = Buffer.concat([decipher.update(ciphertext.subarray(0, -16)), decipher.final()]);
    const value = JSON.parse(plaintext.toString("utf8")) as Record<string, unknown>;
    if (!value || typeof value !== "object") throw new Error("payload");
    const modelBaseUrl = value.model_base_url ?? value.modelBaseUrl;
    const modelApiKey = value.model_api_key ?? value.modelApiKey;
    const datatapToken = value.datatap_token ?? value.datatapToken;
    const datatapUrls = value.datatap_urls ?? value.datatapUrls;
    if (
      typeof modelBaseUrl !== "string" ||
      typeof modelApiKey !== "string" ||
      typeof datatapToken !== "string" ||
      !datatapUrls ||
      typeof datatapUrls !== "object"
    ) throw new Error("payload");
    return { modelBaseUrl, modelApiKey, datatapToken, datatapUrls: datatapUrls as Record<string, string> };
  } catch (error) {
    throw new Error("pi_secret_envelope_invalid", { cause: error });
  }
}
