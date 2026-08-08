/** 真实 Pi CLI 加载 adapter + 审计扩展的本机集成回归。 */

import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawn } from "node:child_process";
import { once } from "node:events";

import { afterEach, describe, expect, it } from "vitest";

const children: ReturnType<typeof spawn>[] = [];
const tempDirectories: string[] = [];

afterEach(async () => {
  for (const child of children.splice(0)) {
    if (child.exitCode === null) child.kill("SIGTERM");
  }
  await Promise.all(tempDirectories.splice(0).map((directory) => rm(directory, { recursive: true, force: true })));
});

async function readJson(request: IncomingMessage): Promise<Record<string, any>> {
  const chunks: Buffer[] = [];
  for await (const chunk of request) chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
  return JSON.parse(Buffer.concat(chunks).toString("utf8")) as Record<string, any>;
}

function reply(response: ServerResponse, status: number, body: unknown, headers: Record<string, string> = {}): void {
  response.writeHead(status, { "content-type": "application/json", ...headers });
  response.end(JSON.stringify(body));
}

describe("真实 Pi Extension loader", () => {
  it("显式加载 adapter 与项目扩展，并在一次 mcp 代理调用后记录 audit_start", async () => {
    const diagnostics: Array<Record<string, unknown>> = [];
    const calls = { list: 0, tool: 0, model: 0, start: 0, settle: 0 };
    const server = createServer(async (request, response) => {
      if (request.method !== "POST") { response.writeHead(202); response.end(); return; }
      let body: Record<string, any>;
      try { body = await readJson(request); } catch { response.writeHead(202); response.end(); return; }
      if (request.url?.startsWith("/mcp/")) {
        if (body.method === "initialize") return reply(response, 200, { jsonrpc: "2.0", id: body.id, result: { protocolVersion: body.params.protocolVersion, capabilities: { tools: {} }, serverInfo: { name: "fake", version: "1" } } }, { "mcp-session-id": "fake" });
        if (body.method === "tools/list") {
          calls.list += 1;
          const tools = request.url === "/mcp/social-grow-content"
            ? [{ name: "hotwords_xiaohongshu_dictionary", inputSchema: { type: "object", properties: {} } }]
            : [];
          return reply(response, 200, { jsonrpc: "2.0", id: body.id, result: { tools } }, { "mcp-session-id": "fake" });
        }
        if (body.method === "tools/call") {
          calls.tool += 1;
          return reply(response, 200, { jsonrpc: "2.0", id: body.id, result: { content: [{ type: "text", text: "fake-data" }] } }, { "mcp-session-id": "fake" });
        }
        response.writeHead(202, { "mcp-session-id": "fake" }); response.end(); return;
      }
      if (request.url?.endsWith("/diagnostics")) { diagnostics.push(body); return reply(response, 200, { ok: true }); }
      if (request.url?.endsWith("/tool-calls/start")) { calls.start += 1; return reply(response, 200, { call_id: "tracked-1" }); }
      if (request.url?.endsWith("/settle")) { calls.settle += 1; return reply(response, 200, { evidence_id: "evidence-1" }); }
      if (request.url === "/v1/chat/completions") {
        calls.model += 1;
        const delta = calls.model === 1
          ? { tool_calls: [{ index: 0, id: "mcp-call-1", type: "function", function: { name: "mcp", arguments: JSON.stringify({ tool: "social_grow_content_hotwords_xiaohongshu_dictionary", args: {}, server: "social-grow-content" }) } }] }
          : { content: "完成" };
        const finish = calls.model === 1 ? "tool_calls" : "stop";
        response.writeHead(200, { "content-type": "text/event-stream" });
        response.write(`data: ${JSON.stringify({ id: `chat-${calls.model}`, object: "chat.completion.chunk", created: 1, model: "fake", choices: [{ index: 0, delta, finish_reason: null }] })}\n\n`);
        response.write(`data: ${JSON.stringify({ id: `chat-${calls.model}`, object: "chat.completion.chunk", created: 1, model: "fake", choices: [{ index: 0, delta: {}, finish_reason: finish }], usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 } })}\n\n`);
        response.end("data: [DONE]\n\n");
        return;
      }
      reply(response, 404, { error: "not_found" });
    });
    server.listen(0, "127.0.0.1");
    await once(server, "listening");
    const address = server.address();
    if (address === null || typeof address === "string") throw new Error("fake_server_address_missing");
    const root = `http://127.0.0.1:${address.port}`;
    const agentDir = await mkdtemp(join(tmpdir(), "pi-adapter-loader-"));
    tempDirectories.push(agentDir);
    await writeFile(join(agentDir, "models.json"), JSON.stringify({ providers: { fake: { baseUrl: `${root}/v1`, apiKey: "$FAKE_MODEL_KEY", authHeader: true, api: "openai-completions", models: [{ id: "fake", input: ["text"] }] } } }));
    await writeFile(join(agentDir, "mcp-cache.json"), '{"version":1,"servers":{}}');
    const cli = resolve("node_modules/@earendil-works/pi-coding-agent/dist/cli.js");
    const adapter = resolve("node_modules/pi-mcp-adapter/index.ts");
    const audit = resolve("src/extensions/poc-runtime.ts");
    const child = spawn(process.execPath, [cli, "--mode", "rpc", "--no-session", "--no-builtin-tools", "--no-context-files", "--no-extensions", "-e", adapter, "-e", audit, "--no-skills", "--provider", "fake", "--model", "fake"], {
      cwd: resolve("."),
      env: {
        PATH: process.env.PATH ?? "", HOME: agentDir, TMPDIR: tmpdir(), LANG: "C", PI_CODING_AGENT_DIR: agentDir, PI_OFFLINE: "1", PI_SKIP_VERSION_CHECK: "1",
        DATATAP_MCP_TOKEN: "fake-datatap-token", DATATAP_INSIGHT_CUBE_MCP_URL: `${root}/mcp/insight-cube`, DATATAP_SOCIAL_GROW_MCP_URL: `${root}/mcp/social-grow`, DATATAP_SOCIAL_GROW_CONTENT_MCP_URL: `${root}/mcp/social-grow-content`, DATATAP_AKTOOLS_MCP_URL: `${root}/mcp/aktools`,
        PI_RUNTIME_POC_BASE_URL: `${root}/api/v1/internal/pi-poc`, PI_RUNTIME_POC_RUN_ID: "fake-run", PI_RUNTIME_POC_TOKEN: "fake-run-token", FAKE_MODEL_KEY: "fake-model-key",
      }, stdio: ["pipe", "pipe", "pipe"],
    });
    children.push(child);
    const stdout: Buffer[] = [];
    const stderr: Buffer[] = [];
    child.stdout.on("data", (chunk: Buffer) => stdout.push(chunk));
    child.stderr.on("data", (chunk: Buffer) => stderr.push(chunk));
    child.stdin.write(`${JSON.stringify({ id: "prompt-1", type: "prompt", message: "执行一次已知 MCP 工具。" })}\n`);
    try {
      try {
        await expect.poll(() => diagnostics.some((entry) => entry.stage === "audit_start"), { timeout: 12_000 }).toBe(true);
        await expect.poll(
          () => calls.list === 1 && calls.tool === 1 && calls.settle === 1,
          { timeout: 12_000 },
        ).toBe(true);
      } catch {
        const record_types = Buffer.concat(stdout).toString("utf8").split("\n").filter(Boolean).flatMap((line) => {
          try {
            const value = JSON.parse(line) as { type?: unknown; event?: { type?: unknown } };
            return [{ type: value.type, event_type: value.event?.type }];
          } catch { return []; }
        });
        throw new Error(JSON.stringify({ calls, record_types, stderr_bytes: Buffer.concat(stderr).length, exit_code: child.exitCode }));
      }
      expect(calls).toMatchObject({ list: 1, tool: 1, start: 1, settle: 1 });
      expect(diagnostics.some((entry) => entry.stage === "audit_start" && entry.tool_name === "hotwords_xiaohongshu_dictionary")).toBe(true);
    } finally {
      if (child.exitCode === null) child.kill("SIGTERM");
      await once(child, "close").catch(() => undefined);
      await new Promise<void>((resolveClose) => server.close(() => resolveClose()));
    }
  }, 20_000);
});
