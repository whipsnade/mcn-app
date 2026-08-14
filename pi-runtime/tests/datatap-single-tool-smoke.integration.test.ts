/** 受版本控制单工具冒烟入口的进程级集成测试；仅使用本机 fake MCP/audit。 */

import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { once } from "node:events";
import { spawn } from "node:child_process";

import { describe, expect, it } from "vitest";

type FakeOptions = { toolError?: boolean };

async function readJson(request: IncomingMessage): Promise<Record<string, unknown>> {
  const chunks: Buffer[] = [];
  for await (const chunk of request) chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
  return JSON.parse(Buffer.concat(chunks).toString("utf8")) as Record<string, unknown>;
}

function reply(response: ServerResponse, status: number, body?: unknown): void {
  response.writeHead(status, { "content-type": "application/json", "mcp-session-id": "fake-session" });
  response.end(body === undefined ? undefined : JSON.stringify(body));
}

async function startFakeServices(options: FakeOptions = {}) {
  const stages: Array<Record<string, unknown>> = [];
  const calls = { list: 0, tool: 0, start: 0, settle: 0, fail: 0, smokeFail: 0, smokeSucceed: 0, running: false };
  const server = createServer(async (request, response) => {
    if (request.method !== "POST") return reply(response, 405, { error: "method" });
    const body = await readJson(request);
    if (request.url === "/mcp") {
      if (body.method === "initialize") {
        return reply(response, 200, {
          jsonrpc: "2.0", id: body.id,
          result: {
            protocolVersion: String((body.params as Record<string, unknown>).protocolVersion),
            capabilities: { tools: {} }, serverInfo: { name: "fake", version: "1" },
          },
        });
      }
      if (body.method === "tools/list") {
        calls.list += 1;
        return reply(response, 200, {
          jsonrpc: "2.0", id: body.id,
          result: { tools: [{ name: "hotwords_xiaohongshu_dictionary", inputSchema: { type: "object", properties: {} } }] },
        });
      }
      if (body.method === "tools/call") {
        calls.tool += 1;
        return reply(response, 200, {
          jsonrpc: "2.0", id: body.id,
          result: options.toolError
            ? { isError: true, content: [{ type: "text", text: "fake_error" }] }
            : { content: [{ type: "text", text: "{\"fake\":true}" }] },
        });
      }
      return reply(response, 202);
    }
    if (request.url?.endsWith("/diagnostics")) {
      stages.push(body);
      return reply(response, 200, { ok: true });
    }
    if (request.url?.endsWith("/tool-calls/start")) {
      calls.start += 1;
      calls.running = true;
      return reply(response, 200, { call_id: "fake-call" });
    }
    if (request.url?.endsWith("/settle")) {
      calls.settle += 1;
      calls.running = false;
      return reply(response, 200, { evidence_id: "fake-evidence" });
    }
    if (request.url?.endsWith("/fail")) {
      calls.fail += 1;
      calls.running = false;
      return reply(response, 200, { ok: true });
    }
    if (request.url?.endsWith("/smoke-failed")) {
      calls.smokeFail += 1;
      calls.running = false;
      return reply(response, 200, { ok: true });
    }
    if (request.url?.endsWith("/smoke-succeeded")) {
      calls.smokeSucceed += 1;
      calls.running = false;
      return reply(response, 200, { ok: true });
    }
    return reply(response, 404, { error: "path" });
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  if (address === null || typeof address === "string") throw new Error("fake_server_address_missing");
  return {
    root: `http://127.0.0.1:${address.port}`,
    stages,
    calls,
    close: async () => new Promise<void>((resolveClose) => server.close(() => resolveClose())),
  };
}

async function runSmoke(env: Record<string, string | undefined>) {
  const child = spawn("npm", ["run", "smoke:datatap"], {
    cwd: process.cwd(),
    env: { PATH: process.env.PATH, HOME: process.env.HOME, ...env },
    stdio: ["ignore", "pipe", "pipe"],
  });
  const stdout: Buffer[] = [];
  const stderr: Buffer[] = [];
  child.stdout.on("data", (chunk: Buffer) => stdout.push(chunk));
  child.stderr.on("data", (chunk: Buffer) => stderr.push(chunk));
  const [code] = await once(child, "close") as [number | null];
  return { code, stdout: Buffer.concat(stdout).toString("utf8"), stderr: Buffer.concat(stderr).toString("utf8") };
}

function smokeEnv(root: string): Record<string, string> {
  return {
    RUN_PI_POC_DATATAP_SMOKE: "1",
    PI_RUNTIME_POC_RUN_ID: "fake-run",
    PI_RUNTIME_POC_TOKEN: "fake-run-token",
    PI_RUNTIME_POC_BASE_URL: `${root}/api/v1/internal/pi-poc`,
    DATATAP_MCP_TOKEN: "fake-datatap-token",
    DATATAP_INSIGHT_CUBE_MCP_URL: `${root}/mcp`,
    DATATAP_SOCIAL_GROW_MCP_URL: `${root}/mcp`,
    DATATAP_SOCIAL_GROW_CONTENT_MCP_URL: `${root}/mcp`,
    DATATAP_AKTOOLS_MCP_URL: `${root}/mcp`,
  };
}

describe("datatap-single-tool-smoke 入口", () => {
  it("实际启动脚本，并且只按固定顺序执行一次 list/call", async () => {
    const fake = await startFakeServices();
    try {
      const result = await runSmoke(smokeEnv(fake.root));
      expect(result.code).toBe(0);
      expect(fake.stages.map((entry) => entry.stage)).toEqual([
        "config", "connect", "tools_list", "schema_validate", "mcp_call", "audit_start", "audit_settle",
      ]);
      expect(fake.calls).toMatchObject({ list: 1, tool: 1, start: 1, settle: 1, fail: 0, smokeFail: 0, smokeSucceed: 1, running: false });
      expect(result.stderr).not.toContain("fake-datatap-token");
      expect(result.stderr).not.toContain(fake.root);
    } finally {
      await fake.close();
    }
  }, 15_000);

  it("缺少 opt-in 时连接前 fail-closed，且不创建 running 调用", async () => {
    const fake = await startFakeServices();
    try {
      const env = smokeEnv(fake.root);
      delete env.RUN_PI_POC_DATATAP_SMOKE;
      const result = await runSmoke(env);
      expect(result.code).not.toBe(0);
      expect(fake.calls).toMatchObject({ list: 0, tool: 0, start: 0, settle: 0, fail: 0, smokeFail: 0, smokeSucceed: 0, running: false });
      expect(result.stderr).toContain('"code":"pi_poc_smoke_opt_in_required"');
      expect(result.stderr).not.toContain("fake-datatap-token");
      expect(result.stderr).not.toContain(fake.root);
    } finally {
      await fake.close();
    }
  }, 15_000);

  it.each([
    ["Run ID", "PI_RUNTIME_POC_RUN_ID", "pi_poc_smoke_run_id_required"],
    ["内部审计地址", "PI_RUNTIME_POC_BASE_URL", "pi_poc_smoke_audit_url_required"],
    ["服务映射", "DATATAP_AKTOOLS_MCP_URL", "pi_poc_adapter_datatap_url_required"],
    ["DataTap 凭证", "DATATAP_MCP_TOKEN", "pi_poc_smoke_datatap_token_required"],
  ])("缺少%s时连接前 fail-closed", async (_label, key, code) => {
    const fake = await startFakeServices();
    try {
      const env = smokeEnv(fake.root);
      delete env[key as keyof typeof env];
      const result = await runSmoke(env);
      expect(result.code).not.toBe(0);
      expect(result.stderr).toContain(`"code":"${code}"`);
      expect(fake.calls).toMatchObject({ list: 0, tool: 0, start: 0, settle: 0, fail: 0, smokeFail: 0, smokeSucceed: 0, running: false });
    } finally {
      await fake.close();
    }
  }, 15_000);

  it("工具错误非零退出并收口 audit 调用，不遗留 running 状态", async () => {
    const fake = await startFakeServices({ toolError: true });
    try {
      const result = await runSmoke(smokeEnv(fake.root));
      expect(result.code).not.toBe(0);
      expect(fake.calls).toMatchObject({ list: 1, tool: 1, start: 1, settle: 0, fail: 1, smokeFail: 1, smokeSucceed: 0, running: false });
      expect(result.stderr).toContain('"stage":"mcp_call"');
      expect(result.stderr).not.toContain("fake-datatap-token");
      expect(result.stderr).not.toContain(fake.root);
    } finally {
      await fake.close();
    }
  }, 15_000);
});
