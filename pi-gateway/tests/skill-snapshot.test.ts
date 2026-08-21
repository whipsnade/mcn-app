import { createHash } from "node:crypto";
import { mkdtemp, readFile, rm, stat, writeFile, symlink } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";

import {
  MAX_CONTENT_BYTES,
  MAX_SKILLS,
  materializeRunSkillSnapshot,
  skillSnapshotDigest,
} from "../src/skill-snapshot.js";
import { createProductionResourceLoader } from "../src/resource-loader.js";
import type { SkillSnapshotEntry } from "../src/protocol.js";

const roots: string[] = [];

async function makeRoot(): Promise<string> {
  const root = await mkdtemp(join(tmpdir(), "pi-skill-test-"));
  roots.push(root);
  return root;
}

function entry(overrides: Partial<SkillSnapshotEntry> = {}): SkillSnapshotEntry {
  const content = "---\nname: campaign-research\ndescription: test\nrequired_tools: []\n---\n\nbody\n";
  return {
    name: "campaign-research",
    revision: 3,
    contentDigest: createHash("sha256").update(content).digest("hex"),
    description: "test",
    requiredTools: [],
    artifactContract: "analysis_report_v1",
    content,
    ...overrides,
  };
}

afterEach(async () => {
  await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
});

describe("run Skill snapshot materializer", () => {
  it("writes only explicit skills with restrictive permissions and stable digest", async () => {
    const root = await makeRoot();
    const snapshotPath = await materializeRunSkillSnapshot(root, [entry()]);
    const skillFile = join(snapshotPath, "campaign-research", "SKILL.md");

    expect(await readFile(skillFile, "utf8")).toContain("name: campaign-research");
    expect((await stat(snapshotPath)).mode & 0o777).toBe(0o700);
    expect((await stat(skillFile)).mode & 0o777).toBe(0o600);
    expect(skillSnapshotDigest(entry())).toBe(entry().contentDigest);
  });

  it("fails closed on digest drift, traversal, symlink and unknown files", async () => {
    const root = await makeRoot();
    await expect(
      materializeRunSkillSnapshot(root, [entry({ contentDigest: "0".repeat(64) })]),
    ).rejects.toThrow("pi_skill_snapshot_digest_mismatch");
    await expect(
      materializeRunSkillSnapshot(root, [entry({ name: "../escape" })]),
    ).rejects.toThrow("pi_skill_snapshot_path_invalid");

    const outside = await mkdtemp(join(tmpdir(), "pi-skill-outside-"));
    await symlink(outside, join(root, ".skill-snapshot"));
    await expect(materializeRunSkillSnapshot(root, [entry()])).rejects.toThrow(
      "pi_skill_snapshot_symlink",
    );
    await rm(join(root, ".skill-snapshot"), { force: true });

    const snapshotPath = await materializeRunSkillSnapshot(root, [entry()]);
    await writeFile(join(snapshotPath, "unexpected.txt"), "unexpected", { mode: 0o600 });
    await expect(materializeRunSkillSnapshot(root, [entry()])).rejects.toThrow(
      "pi_skill_snapshot_unknown_file",
    );
    await rm(outside, { recursive: true, force: true });
  });

  it("loads the explicit snapshot path while leaving SDK discovery disabled", async () => {
    const root = await makeRoot();
    const snapshotPath = await materializeRunSkillSnapshot(root, [entry()]);
    const loader = createProductionResourceLoader({
      cwd: root,
      agentDir: join(root, "agent"),
      rootPolicy: "ROOT POLICY",
      additionalSkillPaths: [snapshotPath],
    });

    await loader.reload();
    expect(loader.getSkills().skills.map((skill) => skill.name)).toEqual(["campaign-research"]);
    expect(loader.getSystemPrompt()).toBe("ROOT POLICY");
  });

  it("enforces the shared skill count and UTF-8 byte limits", async () => {
    const root = await makeRoot();
    const largeContent = "中".repeat(Math.ceil(MAX_CONTENT_BYTES / 2));
    await expect(
      materializeRunSkillSnapshot(root, [entry({ content: largeContent })]),
    ).rejects.toThrow("pi_skill_snapshot_entry_invalid");

    const entries = Array.from({ length: MAX_SKILLS + 1 }, (_, index) => {
      const name = `skill-${index.toString().padStart(2, "0")}`;
      const content = `---\nname: ${name}\ndescription: test\nrequired_tools: []\n---\nbody\n`;
      return entry({
        name,
        content,
        contentDigest: createHash("sha256").update(content).digest("hex"),
      });
    });
    await expect(materializeRunSkillSnapshot(root, entries)).rejects.toThrow(
      "pi_skill_snapshot_invalid",
    );
  });
});
