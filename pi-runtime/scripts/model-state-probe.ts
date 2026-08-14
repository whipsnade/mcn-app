import { spawn } from "node:child_process";
import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import { join } from "node:path";

import { buildPiModelConfig } from "../src/rpc/model-config.ts";
import { parseRpcLine, type PiRpcRecord } from "../src/rpc/protocol.ts";

const piBinary = fileURLToPath(new URL("../node_modules/.bin/pi", import.meta.url));
const stateRequest = { id: "poc-model-state", type: "get_state" } as const;
const abortRequest = { id: "poc-model-abort", type: "abort" } as const;
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
      finish(() => reject(new Error("pi_model_probe_timeout")));
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

function responseFor(records: PiRpcRecord[], id: string, command: string) {
  const response = records.find((record) => record.type === "response" && record.id === id);
  if (!response || response.command !== command || response.success !== true) {
    throw new Error("invalid_rpc_response");
  }
  return response;
}

async function main() {
  const model = process.env.TENCENT_PLAN_MODEL;
  const baseUrl = process.env.TENCENT_PLAN_BASE_URL;
  const thinking = process.env.TENCENT_PLAN_REASONING_EFFORT;
  const config = buildPiModelConfig({
    baseUrl: baseUrl ?? "",
    model: model ?? "",
    thinking: thinking ?? "",
    apiKeyPresent: Boolean(process.env.TENCENT_PLAN_API_KEY),
  });
  const temporaryRoot = await mkdtemp(join(tmpdir(), "kol_insight_pi_model_state_"));
  const agentDir = join(temporaryRoot, "agent");

  try {
    await mkdir(agentDir, { recursive: true });
    await writeFile(join(agentDir, "models.json"), `${JSON.stringify(config)}\n`, { mode: 0o600 });
    const result = await runPi(
      [
        "--mode",
        "rpc",
        "--no-session",
        "--no-builtin-tools",
        "--no-context-files",
        "--no-extensions",
        "--no-skills",
        "--provider",
        "kol_insight_pi_poc",
        "--model",
        model as string,
        "--thinking",
        thinking as string,
      ],
      `${JSON.stringify(stateRequest)}\n${JSON.stringify(abortRequest)}\n`,
      { ...process.env, PI_CODING_AGENT_DIR: agentDir, PI_OFFLINE: "1" },
    );
    if (result.exitCode !== 0 || result.stderr) {
      throw new Error("pi_model_probe_failed");
    }

    const records = result.stdout
      .split("\n")
      .filter(Boolean)
      .map((line) => parseRpcLine(line));
    const state = responseFor(records, stateRequest.id, stateRequest.type);
    responseFor(records, abortRequest.id, abortRequest.type);
    const actualModel = state.data as { model?: { id?: unknown; provider?: unknown }; thinkingLevel?: unknown };
    if (
      actualModel.model?.provider !== "kol_insight_pi_poc" ||
      actualModel.model?.id !== model ||
      actualModel.thinkingLevel !== thinking
    ) {
      throw new Error("runtime_model_mismatch");
    }

    console.log(
      JSON.stringify({
        actualModel: actualModel.model.id,
        actualProvider: actualModel.model.provider,
        actualThinking: actualModel.thinkingLevel,
        responseShape: records
          .filter((record) => record.type === "response")
          .map(({ command, id, success, type }) => ({ command, id, success, type })),
        stderrBytes: 0,
        stdoutJsonlOnly: true,
      }),
    );
  } finally {
    await rm(temporaryRoot, { force: true, recursive: true });
  }
}

await main();
