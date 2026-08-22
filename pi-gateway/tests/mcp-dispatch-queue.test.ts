import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  createMcpAccountingExtensionFactory,
  McpAccountingExtension,
  readMcpDispatchGateOptions,
} from "../src/mcp-accounting-extension.js";

function hooks() {
  const handlers = new Map<string, (event: any) => Promise<unknown>>();
  const install = (extension: McpAccountingExtension, bindings = [
    { toolName: "chain_probe_tool", server: "insight-cube-mcp", remoteName: "probe" },
    { toolName: "social_probe_tool", server: "social-grow-mcp", remoteName: "social" },
  ], gateOptions?: { maxInflight?: number; slotWaitMs?: number; callTimeoutMs?: number }) => {
    createMcpAccountingExtensionFactory(extension, bindings, gateOptions)({
      on: (name: string, handler: (event: any) => Promise<unknown>) => handlers.set(name, handler),
    } as any);
  };
  return { handlers, install };
}

function toolCallEvent(id: string, tool = "chain_probe_tool") {
  return { type: "tool_call", toolName: "mcp", toolCallId: id, input: { tool, server: "", args: {} } };
}

function toolResultEvent(id: string, isError = false, error?: string) {
  return {
    type: "tool_result", toolName: "mcp", toolCallId: id,
    content: [{ type: "text", text: "ok" }], isError,
    details: error === undefined ? {} : { error },
  };
}

/** 用受控 deferred 驱动 preflight/finalize/fail 的时序。 */
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

describe("per-server mcp dispatch gate", () => {
  it("serializes concurrent calls to the same server", async () => {
    const preflightOrder: string[] = [];
    const firstPreflight = deferred<{ permit_id: string }>();
    const preflight = vi.fn(async (input: { tool: string }) => {
      preflightOrder.push(input.tool);
      if (input.tool === "chain_probe_tool") return firstPreflight.promise;
      return { permit_id: `permit-${input.tool}` };
    });
    const finalize = vi.fn(async () => ({ ok: true }));
    const extension = new McpAccountingExtension({ preflight, finalize, fail: vi.fn() });
    const { handlers, install } = hooks();
    install(extension);

    const first = handlers.get("tool_call")?.(toolCallEvent("call-1"));
    const second = handlers.get("tool_call")?.(toolCallEvent("call-2"));
    await Promise.resolve();
    // 第二个调用在同 server 槽位上等待：它的 preflight 尚未被调用。
    expect(preflight).toHaveBeenCalledTimes(1);
    expect(preflightOrder).toEqual(["chain_probe_tool"]);

    firstPreflight.resolve({ permit_id: "permit-1" });
    await first;
    // 第一个 preflight 完成但仍未 tool_result → 第二个依旧等待。
    expect(preflight).toHaveBeenCalledTimes(1);

    await handlers.get("tool_result")?.(toolResultEvent("call-1"));
    await second;
    expect(preflight).toHaveBeenCalledTimes(2);
    expect(preflightOrder).toEqual(["chain_probe_tool", "chain_probe_tool"]);
  });

  it("does not block different servers", async () => {
    const firstPreflight = deferred<{ permit_id: string }>();
    const preflight = vi.fn(async (input: { tool: string; server: string }) => {
      if (input.server === "insight-cube-mcp") return firstPreflight.promise;
      return { permit_id: "permit-social" };
    });
    const extension = new McpAccountingExtension({ preflight, finalize: vi.fn(async () => ({ ok: true })), fail: vi.fn() });
    const { handlers, install } = hooks();
    install(extension);

    const first = handlers.get("tool_call")?.(toolCallEvent("call-1"));
    const second = handlers.get("tool_call")?.(toolCallEvent("call-social", "social_probe_tool"));
    await Promise.resolve();
    await second;
    expect(preflight).toHaveBeenCalledTimes(2);
    firstPreflight.resolve({ permit_id: "permit-1" });
    await first;
    await handlers.get("tool_result")?.(toolResultEvent("call-1"));
    await handlers.get("tool_result")?.(toolResultEvent("call-social"));
  });

  it("times out waiting calls before preflight with zero billing", async () => {
    const firstPreflight = deferred<{ permit_id: string }>();
    const preflight = vi.fn(async () => firstPreflight.promise);
    const extension = new McpAccountingExtension({ preflight, finalize: vi.fn(), fail: vi.fn() });
    const { handlers, install } = hooks();
    install(extension, undefined, { slotWaitMs: 10 });

    const first = handlers.get("tool_call")?.(toolCallEvent("call-1"));
    const second = await handlers.get("tool_call")?.(toolCallEvent("call-2"));
    expect(second).toEqual({ block: true, reason: "mcp_server_busy" });
    // 第二个调用从未进入 preflight：无 ToolCall、无预留、零计费。
    expect(preflight).toHaveBeenCalledTimes(1);

    // 第一个调用正常完成并释放槽位；超时后的后续调用可以继续获取槽位。
    firstPreflight.resolve({ permit_id: "permit-1" });
    await first;
    await handlers.get("tool_result")?.(toolResultEvent("call-1"));
    const third = handlers.get("tool_call")?.(toolCallEvent("call-3"));
    await Promise.resolve();
    expect(preflight).toHaveBeenCalledTimes(2);
    await third;
  });

  it.each([
    ["success", { finalizeOk: true }],

    ["classified failure", { resultError: "tool_error" }],

    ["ack loss", { finalizeThrows: true, failThrows: true }],

    ["preflight block", { preflightBlock: true }],
  ] as const)("releases the slot on every tool_result exit (%s)", async (_name, spec) => {
    const preflight = vi.fn(async (): Promise<{ block: true; reason: string } | { permit_id: string }> => (
      "preflightBlock" in spec && spec.preflightBlock
        ? { block: true, reason: "mcp_tool_not_allowed" }
        : { permit_id: "permit-1" }
    ));
    const finalize = vi.fn(async () => {
      if ("finalizeThrows" in spec && spec.finalizeThrows) throw new Error("worker_rpc_timeout");
      return { ok: true };
    });
    const fail = vi.fn(async () => {
      if ("failThrows" in spec && spec.failThrows) throw new Error("worker_rpc_timeout");
      return { ok: true };
    });
    const extension = new McpAccountingExtension({ preflight, finalize, fail });
    const { handlers, install } = hooks();
    install(extension);

    if ("preflightBlock" in spec && spec.preflightBlock) {
      // preflight block 路径：槽位立即释放，后续调用可继续进入 preflight。
      const first = await handlers.get("tool_call")?.(toolCallEvent("call-1"));
      expect(first).toEqual({ block: true, reason: "mcp_tool_not_allowed" });
      const second = await handlers.get("tool_call")?.(toolCallEvent("call-2"));
      expect(second).toEqual({ block: true, reason: "mcp_tool_not_allowed" });
      expect(preflight).toHaveBeenCalledTimes(2);
      return;
    }

    const first = handlers.get("tool_call")?.(toolCallEvent("call-1"));
    await first;
    const second = handlers.get("tool_call")?.(toolCallEvent("call-2"));
    await Promise.resolve();
    // 第二个调用在同 server 槽位上等待，尚未进入 preflight。
    expect(preflight).toHaveBeenCalledTimes(1);

    const isError = "resultError" in spec;
    const error = "resultError" in spec ? spec.resultError : undefined;
    await handlers.get("tool_result")?.(toolResultEvent("call-1", isError, error as string | undefined));
    await second;
    await handlers.get("tool_result")?.(toolResultEvent("call-2", isError, error as string | undefined));

    // 两次出口后槽位必须可用：第三个调用能立即进入 preflight。
    const third = await handlers.get("tool_call")?.(toolCallEvent("call-3"));
    expect(preflight).toHaveBeenCalledTimes(3);
    expect(third).toBeUndefined();
  });

  it("defaults to concurrency 1 and honors maxInflight 2", async () => {
    const gate1 = vi.fn();
    const preflight = vi.fn(async () => new Promise<{ permit_id: string }>((resolve) => {
      gate1();
      setTimeout(() => resolve({ permit_id: "permit-1" }), 0);
    }));
    const extension = new McpAccountingExtension({ preflight, finalize: vi.fn(async () => ({ ok: true })), fail: vi.fn() });
    const { handlers, install } = hooks();
    install(extension, undefined, { slotWaitMs: 5 });

    const a = handlers.get("tool_call")?.(toolCallEvent("call-1"));
    const b = await handlers.get("tool_call")?.(toolCallEvent("call-2"));
    expect(b).toEqual({ block: true, reason: "mcp_server_busy" });
    await a;

    const extension2 = new McpAccountingExtension({ preflight, finalize: vi.fn(async () => ({ ok: true })), fail: vi.fn() });
    const second = hooks();
    second.install(extension2, undefined, { maxInflight: 2, slotWaitMs: 5 });
    const c = second.handlers.get("tool_call")?.(toolCallEvent("call-3"));
    const d = second.handlers.get("tool_call")?.(toolCallEvent("call-4"));
    await Promise.resolve();
    await Promise.resolve();
    // maxInflight=2：两个并发 preflight 同时挂起（未收到 mcp_server_busy）。
    const dResult = await Promise.race([d, new Promise((r) => setTimeout(() => r("busy"), 30))]);
    expect(dResult).not.toEqual({ block: true, reason: "mcp_server_busy" });
    await c;
    if (dResult !== "busy") await d;
  });

  it("reads gate tuning from non-secret env keys", () => {
    expect(readMcpDispatchGateOptions({})).toEqual({});
    expect(readMcpDispatchGateOptions({ PI_MCP_SERVER_MAX_INFLIGHT: "2" })).toEqual({ maxInflight: 2 });
    expect(readMcpDispatchGateOptions({ PI_MCP_SLOT_WAIT_MS: "1500" })).toEqual({ slotWaitMs: 1500 });
    expect(readMcpDispatchGateOptions({ PI_MCP_SERVER_MAX_INFLIGHT: "0" })).toEqual({});
    expect(readMcpDispatchGateOptions({ PI_MCP_SERVER_MAX_INFLIGHT: "abc" })).toEqual({});
  });

  it("keeps free discovery tools off the gate", async () => {
    const preflight = vi.fn(async () => ({ permit_id: "permit-1" }));
    const extension = new McpAccountingExtension({ preflight, finalize: vi.fn(), fail: vi.fn() });
    const { handlers, install } = hooks();
    install(extension, [
      { toolName: "chain_probe_tool", server: "insight-cube-mcp", remoteName: "probe" },
      { toolName: "social_probe_tool", server: "social-grow-mcp", remoteName: "social" },
      // 免费发现工具必须先通过身份解析才能进入免费路径：绑定里显式提供。
      { toolName: "search", server: "social-grow-mcp", remoteName: "search" },
    ]);

    // 占住 insight-cube 槽位的调用挂起在 preflight 上。
    const firstPreflight = deferred<{ permit_id: string }>();
    preflight.mockImplementationOnce(async () => firstPreflight.promise);
    const occupying = handlers.get("tool_call")?.(toolCallEvent("call-1"));
    await Promise.resolve();

    // search 是免费发现工具（social-grow server）：不占槽，即使有其它
    // server 的槽位被占用也能通过，且不触发 preflight 计费路径。
    const freeCall = await handlers.get("tool_call")?.(toolCallEvent("call-2", "search"));
    expect(freeCall).toBeUndefined();
    expect(preflight).toHaveBeenCalledTimes(1);

    firstPreflight.resolve({ permit_id: "permit-1" });
    await occupying;
    await handlers.get("tool_result")?.(toolResultEvent("call-1"));
  });
});

describe("wall-clock watchdog for in-flight mcp calls", () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  it("releases the queue slot after the wall clock without touching billing", async () => {
    const firstPreflight = deferred<{ permit_id: string }>();
    const preflight = vi.fn(async () => firstPreflight.promise);
    const finalize = vi.fn(async () => ({ ok: true }));
    const fail = vi.fn(async () => ({ ok: true }));
    const extension = new McpAccountingExtension({ preflight, finalize, fail });
    const { handlers, install } = hooks();
    install(extension, undefined, { callTimeoutMs: 1_000, slotWaitMs: 300_000 });

    const zombie = handlers.get("tool_call")?.(toolCallEvent("call-1"));
    await Promise.resolve();
    firstPreflight.resolve({ permit_id: "permit-1" });
    await zombie;

    // 排队的第二个调用：墙钟内等待槽位。
    const second = handlers.get("tool_call")?.(toolCallEvent("call-2"));
    await Promise.resolve();
    expect(preflight).toHaveBeenCalledTimes(1);

    // 超过墙钟：僵尸调用的槽位被看门狗释放，第二个调用被放行。
    vi.advanceTimersByTime(1_000);
    await Promise.resolve();
    await Promise.resolve();
    expect(preflight).toHaveBeenCalledTimes(2);
    await second;

    // 看门狗只管活性：fail/finalize 从未被调用（账务不动，等真实 tool_result）。
    expect(finalize).not.toHaveBeenCalled();
    expect(fail).not.toHaveBeenCalled();
  });

  it("keeps settle semantics for a late tool_result and never double-releases", async () => {
    const firstPreflight = deferred<{ permit_id: string }>();
    const secondPreflight = deferred<{ permit_id: string }>();
    let calls = 0;
    const preflight = vi.fn(async () => {
      calls += 1;
      return calls === 1 ? firstPreflight.promise : secondPreflight.promise;
    });
    const finalize = vi.fn(async () => ({ ok: true }));
    const extension = new McpAccountingExtension({ preflight, finalize, fail: vi.fn() });
    const { handlers, install } = hooks();
    install(extension, undefined, { callTimeoutMs: 1_000, slotWaitMs: 300_000 });

    const zombie = handlers.get("tool_call")?.(toolCallEvent("call-1"));
    await Promise.resolve();
    firstPreflight.resolve({ permit_id: "permit-1" });
    await zombie;
    vi.advanceTimersByTime(1_000); // 看门狗释放僵尸槽位

    const second = handlers.get("tool_call")?.(toolCallEvent("call-2"));
    await Promise.resolve();
    await Promise.resolve();
    secondPreflight.resolve({ permit_id: "permit-2" });
    await second;

    // 晚到的僵尸 tool_result：正常 finalize settle；releaseSlot 为幂等 no-op。
    await handlers.get("tool_result")?.(toolResultEvent("call-1"));
    expect(finalize).toHaveBeenCalledWith(
      { permit_id: "permit-1" },
      expect.objectContaining({ outcome: "succeeded" }),
    );

    // 无二次释放：call-2 仍持有唯一槽位，call-3 必须等待 call-2 的 tool_result。
    const third = handlers.get("tool_call")?.(toolCallEvent("call-3"));
    await Promise.resolve();
    expect(preflight).toHaveBeenCalledTimes(2); // call-3 未进入 preflight
    await handlers.get("tool_result")?.(toolResultEvent("call-2"));
    await Promise.resolve();
    await Promise.resolve();
    expect(preflight).toHaveBeenCalledTimes(3); // call-3 现在被放行
    await third;
  });

  it("clears the timer on natural tool_result (no watchdog side effects)", async () => {
    const preflight = vi.fn(async () => ({ permit_id: "permit-1" }));
    const extension = new McpAccountingExtension({ preflight, finalize: vi.fn(async () => ({ ok: true })), fail: vi.fn() });
    const { handlers, install } = hooks();
    install(extension, undefined, { callTimeoutMs: 1_000, slotWaitMs: 300_000 });

    const first = handlers.get("tool_call")?.(toolCallEvent("call-1"));
    await first;
    const timersAfterCall = vi.getTimerCount();
    expect(timersAfterCall).toBeGreaterThanOrEqual(1);

    await handlers.get("tool_result")?.(toolResultEvent("call-1"));
    await first;
    // 计时器被清除：自然完成后无残留看门狗。
    expect(vi.getTimerCount()).toBe(timersAfterCall - 1);

    // 超时推进不再触发任何释放：call-2 拿到槽位后保持占用。
    const second = handlers.get("tool_call")?.(toolCallEvent("call-2"));
    await Promise.resolve();
    vi.advanceTimersByTime(5_000);
    await Promise.resolve();
    const third = handlers.get("tool_call")?.(toolCallEvent("call-3"));
    await Promise.resolve();
    await Promise.resolve();
    expect(preflight).toHaveBeenCalledTimes(2); // call-3 未被误放行
    await second;
  });

  it("defaults to 150s and clamps the env override into 1..600s", async () => {
    const preflight = vi.fn(async () => ({ permit_id: "permit-1" }));
    const extension = new McpAccountingExtension({ preflight, finalize: vi.fn(async () => ({ ok: true })), fail: vi.fn() });
    const { handlers, install } = hooks();
    install(extension, undefined, { slotWaitMs: 300_000 });

    const first = handlers.get("tool_call")?.(toolCallEvent("call-1"));
    await first;
    // 默认 150s：149_999 未触发，150_000 触发。
    vi.advanceTimersByTime(149_999);
    const second = handlers.get("tool_call")?.(toolCallEvent("call-2"));
    await Promise.resolve();
    expect(preflight).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(1);
    await Promise.resolve();
    await Promise.resolve();
    expect(preflight).toHaveBeenCalledTimes(2);
    await second;

    // env 解析：合法覆盖、上下钳制、非法回退默认（缺省键）。
    expect(readMcpDispatchGateOptions({ PI_MCP_CALL_TIMEOUT_MS: "30000" })).toMatchObject({ callTimeoutMs: 30_000 });
    expect(readMcpDispatchGateOptions({ PI_MCP_CALL_TIMEOUT_MS: "0" })).toMatchObject({ callTimeoutMs: 1 });
    expect(readMcpDispatchGateOptions({ PI_MCP_CALL_TIMEOUT_MS: "999999999" })).toMatchObject({ callTimeoutMs: 600_000 });
    expect(readMcpDispatchGateOptions({ PI_MCP_CALL_TIMEOUT_MS: "abc" })).toEqual({});
    expect(readMcpDispatchGateOptions({})).toEqual({});
  });

  it("unrefs the watchdog timer so it never blocks worker exit", async () => {
    const unrefSpies: Array<ReturnType<typeof vi.fn>> = [];
    const originalSetTimeout = globalThis.setTimeout;
    const wrappedSetTimeout = ((handler: (...args: unknown[]) => void, timeout?: number, ...rest: unknown[]) => {
      const handle = originalSetTimeout(handler, timeout, ...rest);
      const unrefSpy = vi.fn();
      const originalUnref = (handle as unknown as { unref?: () => void }).unref;
      if (typeof originalUnref === "function") {
        (handle as unknown as { unref: () => void }).unref = () => {
          unrefSpy();
          originalUnref.call(handle);
        };
      }
      unrefSpies.push(unrefSpy);
      return handle;
    }) as typeof setTimeout;
    globalThis.setTimeout = wrappedSetTimeout;
    try {
      const preflight = vi.fn(async () => ({ permit_id: "permit-1" }));
      const extension = new McpAccountingExtension({ preflight, finalize: vi.fn(async () => ({ ok: true })), fail: vi.fn() });
      const { handlers, install } = hooks();
      install(extension, undefined, { callTimeoutMs: 1_000, slotWaitMs: 300_000 });
      const first = handlers.get("tool_call")?.(toolCallEvent("call-1"));
      await Promise.resolve();
      await first;
      expect(unrefSpies.some((spy) => spy.mock.calls.length > 0)).toBe(true);
    } finally {
      globalThis.setTimeout = originalSetTimeout;
    }
  });
});
