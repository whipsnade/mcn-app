import { spawn } from "node:child_process";
import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import { join } from "node:path";

import {
  assertAuditTranscript,
  makeAuditRequests,
  rpcProbeResources,
} from "../src/rpc/audit.ts";
import { parseRpcLine } from "../src/rpc/protocol.ts";

const piBinary = fileURLToPath(new URL("../node_modules/.bin/pi", import.meta.url));
const probeTimeoutMs = 10_000;

function runPi(args: string[], input: string, env: NodeJS.ProcessEnv) {
  return new Promise<{ exitCode: number | null; stderr: string; stdout: string }>((resolve, reject) => {
    const child = spawn(piBinary, args, { env, stdio: ["pipe", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    let settled = false;
    const finish = (callback: () => void) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      callback();
    };
    const timeout = setTimeout(() => {
      child.kill("SIGTERM");
      finish(() => reject(new Error("pi_rpc_probe_timeout")));
    }, probeTimeoutMs);

    child.stdout.on("data", (chunk: Buffer) => {
      stdout += chunk.toString("utf8");
    });
    child.stderr.on("data", (chunk: Buffer) => {
      stderr += chunk.toString("utf8");
    });
    child.once("error", (error) => finish(() => reject(error)));
    child.once("close", (exitCode) => finish(() => resolve({ exitCode, stderr, stdout })));
    child.stdin.end(input);
  });
}

const extensionSource = (commandName: string) => `
export default function (pi) {
  pi.registerCommand("${commandName}", {
    description: "POC resource loading audit fixture",
    handler: async () => {},
  });
}
`;

async function main() {
  const temporaryRoot = await mkdtemp(join(tmpdir(), "kol_insight_pi_rpc_audit_"));
  const agentDir = join(temporaryRoot, "agent");
  const explicitSkillDir = join(temporaryRoot, "poc-explicit-skill");
  const explicitExtension = join(temporaryRoot, "poc-explicit-extension.mjs");

  try {
    await mkdir(join(agentDir, "extensions"), { recursive: true });
    await mkdir(join(agentDir, "skills", "poc-auto-skill"), { recursive: true });
    await mkdir(explicitSkillDir, { recursive: true });
    await writeFile(
      join(agentDir, "extensions", "poc-auto-extension.mjs"),
      extensionSource(rpcProbeResources.automaticExtension),
    );
    await writeFile(
      join(agentDir, "skills", "poc-auto-skill", "SKILL.md"),
      "---\nname: poc-auto-skill\ndescription: Automatic resource fixture.\n---\n\nMust not load during this probe.\n",
    );
    await writeFile(explicitExtension, extensionSource(rpcProbeResources.explicitExtension));
    await writeFile(
      join(explicitSkillDir, "SKILL.md"),
      "---\nname: poc-explicit-skill\ndescription: Explicit resource fixture.\n---\n\nLoads only through --skill.\n",
    );

    const requests = makeAuditRequests();
    const result = await runPi(
      [
        "--mode",
        "rpc",
        "--no-session",
        "--no-builtin-tools",
        "--no-context-files",
        "--no-extensions",
        "-e",
        explicitExtension,
        "--no-skills",
        "--skill",
        join(explicitSkillDir, "SKILL.md"),
      ],
      `${requests.map((request) => JSON.stringify(request)).join("\n")}\n`,
      { ...process.env, PI_CODING_AGENT_DIR: agentDir, PI_OFFLINE: "1" },
    );
    if (result.exitCode !== 0 || result.stderr) {
      throw new Error("pi_rpc_probe_failed");
    }

    const records = result.stdout
      .split("\n")
      .filter(Boolean)
      .map((line) => parseRpcLine(line));
    const audit = assertAuditTranscript(requests, records);
    const responseShape = records
      .filter((record) => record.type === "response")
      .map(({ command, id, success, type }) => ({ command, id, success, type }));
    const state = records.find((record) => record.id === "poc-get-state");
    const commandsResponse = records.find((record) => record.id === "poc-get-commands");
    const resourceShape =
      commandsResponse && typeof commandsResponse.data === "object" && commandsResponse.data !== null
        ? ((commandsResponse.data as { commands?: unknown }).commands as unknown[] | undefined)
            ?.map((command) => command as { name?: unknown; source?: unknown })
            .filter(
              (command): command is { name: string; source: string } =>
                typeof command.name === "string" && typeof command.source === "string",
            )
            .filter((command) => audit.explicitResources.includes(command.name))
            .map(({ name, source }) => ({ name, source }))
        : [];

    console.log(
      JSON.stringify({
        ...audit,
        automaticResourcesAbsent: true,
        getStateDataKeys:
          state && typeof state.data === "object" && state.data !== null
            ? Object.keys(state.data)
            : [],
        requests,
        resourceShape,
        responseShape,
        stderrBytes: 0,
      }),
    );
  } finally {
    await rm(temporaryRoot, { force: true, recursive: true });
  }
}

await main();
