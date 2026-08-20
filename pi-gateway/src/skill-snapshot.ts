import { createHash } from "node:crypto";
import {
  chmod,
  lstat,
  mkdir,
  mkdtemp,
  open,
  readdir,
  readFile,
  rename,
  rm,
  writeFile,
} from "node:fs/promises";
import { isAbsolute, join, resolve } from "node:path";

import type { SkillSnapshotEntry } from "./protocol.js";

export const RUN_SKILL_SNAPSHOT_DIR = ".skill-snapshot";
const MAX_SKILLS = 128;
const MAX_CONTENT_BYTES = 200_000;
const SKILL_NAME = /^[a-z][a-z0-9-]{1,95}$/;
const SHA256 = /^[0-9a-f]{64}$/;

export function skillSnapshotDigest(entry: Pick<SkillSnapshotEntry, "content">): string {
  return createHash("sha256").update(entry.content, "utf8").digest("hex");
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => `${JSON.stringify(key)}:${stableJson(item)}`)
      .join(",")}}`;
  }
  return JSON.stringify(value) ?? "null";
}

export function skillManifestDigest(
  entries: readonly SkillSnapshotEntry[],
  sourceScope: "database_activation" | "legacy_pack",
): string {
  const payload = {
    source_scope: sourceScope,
    entries: entries.map((entry) => ({
      name: entry.name,
      revision: entry.revision,
      content_digest: entry.contentDigest,
      description: entry.description,
      required_tools: [...entry.requiredTools],
      artifact_contract: entry.artifactContract,
      content: entry.content,
    })),
  };
  return createHash("sha256").update(stableJson(payload), "utf8").digest("hex");
}

function validateEntry(entry: SkillSnapshotEntry): void {
  if (
    entry.name.includes("/") ||
    entry.name.includes("\\") ||
    entry.name === "." ||
    entry.name === ".." ||
    isAbsolute(entry.name)
  ) {
    throw new Error("pi_skill_snapshot_path_invalid");
  }
  if (
    !SKILL_NAME.test(entry.name) ||
    !Number.isInteger(entry.revision) ||
    entry.revision < 1 ||
    !SHA256.test(entry.contentDigest) ||
    typeof entry.description !== "string" ||
    entry.description.length < 1 ||
    entry.description.length > 512 ||
    !Array.isArray(entry.requiredTools) ||
    entry.requiredTools.some((tool) => typeof tool !== "string" || tool.length === 0) ||
    new Set(entry.requiredTools).size !== entry.requiredTools.length ||
    (entry.artifactContract !== null &&
      (typeof entry.artifactContract !== "string" || entry.artifactContract.length === 0)) ||
    typeof entry.content !== "string" ||
    new TextEncoder().encode(entry.content).byteLength > MAX_CONTENT_BYTES
  ) {
    throw new Error("pi_skill_snapshot_entry_invalid");
  }
  if (skillSnapshotDigest(entry) !== entry.contentDigest) {
    throw new Error("pi_skill_snapshot_digest_mismatch");
  }
}

async function validateExistingSnapshot(
  snapshotPath: string,
  expectedNames: ReadonlySet<string>,
): Promise<boolean> {
  let snapshotStat;
  try {
    snapshotStat = await lstat(snapshotPath);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return false;
    throw new Error("pi_skill_snapshot_path_invalid", { cause: error });
  }
  if (snapshotStat.isSymbolicLink()) throw new Error("pi_skill_snapshot_symlink");
  if (!snapshotStat.isDirectory()) throw new Error("pi_skill_snapshot_path_invalid");
  const children = await readdir(snapshotPath, { withFileTypes: true });
  for (const child of children) {
    if (!expectedNames.has(child.name)) {
      throw new Error("pi_skill_snapshot_unknown_file");
    }
    if (child.isSymbolicLink() || !child.isDirectory()) {
      throw new Error("pi_skill_snapshot_symlink");
    }
    const files = await readdir(join(snapshotPath, child.name), { withFileTypes: true });
    if (
      files.length !== 1 ||
      files[0].name !== "SKILL.md" ||
      files[0].isSymbolicLink() ||
      !files[0].isFile()
    ) {
      throw new Error("pi_skill_snapshot_unknown_file");
    }
  }
  return true;
}

async function syncFile(path: string): Promise<void> {
  const handle = await open(path, "r");
  try {
    await handle.sync();
  } finally {
    await handle.close();
  }
}

/**
 * Materialize the server-validated Run manifest into a private, atomic SDK
 * resource directory.  The function has no source-directory fallback.
 */
export async function materializeRunSkillSnapshot(
  rootDir: string,
  entries: readonly SkillSnapshotEntry[],
): Promise<string> {
  if (!isAbsolute(rootDir) || entries.length > MAX_SKILLS) {
    throw new Error("pi_skill_snapshot_invalid");
  }
  const resolvedRoot = resolve(rootDir);
  const rootStat = await lstat(resolvedRoot);
  if (rootStat.isSymbolicLink() || !rootStat.isDirectory()) {
    throw new Error("pi_skill_snapshot_path_invalid");
  }
  const seen = new Set<string>();
  for (const entry of entries) {
    validateEntry(entry);
    if (seen.has(entry.name)) throw new Error("pi_skill_snapshot_duplicate");
    seen.add(entry.name);
  }

  const finalPath = join(resolvedRoot, RUN_SKILL_SNAPSHOT_DIR);
  if (await validateExistingSnapshot(finalPath, seen)) {
    throw new Error("pi_skill_snapshot_already_materialized");
  }
  const temporaryPath = await mkdtemp(join(resolvedRoot, ".skill-snapshot.tmp-"));
  await chmod(temporaryPath, 0o700);
  try {
    for (const entry of entries) {
      const skillDir = join(temporaryPath, entry.name);
      await mkdir(skillDir, { mode: 0o700 });
      await chmod(skillDir, 0o700);
      const skillFile = join(skillDir, "SKILL.md");
      await writeFile(skillFile, entry.content, { encoding: "utf8", mode: 0o600, flag: "wx" });
      await chmod(skillFile, 0o600);
      const persisted = await readFile(skillFile, "utf8");
      if (skillSnapshotDigest({ content: persisted }) !== entry.contentDigest) {
        throw new Error("pi_skill_snapshot_digest_mismatch");
      }
      await syncFile(skillFile);
    }
    await syncFile(temporaryPath);
    await rename(temporaryPath, finalPath);
    return finalPath;
  } catch (error) {
    await rm(temporaryPath, { recursive: true, force: true });
    throw error;
  }
}
