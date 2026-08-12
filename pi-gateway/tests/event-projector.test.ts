import { describe, expect, it } from "vitest";

import { ControlPlaneClient } from "../src/control-plane-client.js";
import { PiSdkUsageProjector, projectPiSdkEvent } from "../src/event-projector.js";
import { parsePiGatewaySourceEvent } from "../src/protocol.js";

describe("Pi SDK usage projector", () => {
  it("projects only bounded run, thinking, text and tool fields in SDK order", () => {
    const projector = new PiSdkUsageProjector("attempt-events");
    const events = [
      { type: "agent_start", eventId: "turn-1" },
      { type: "turn_start", eventId: "turn-1a" },
      { type: "message_update", eventId: "msg-1", assistantMessageEvent: { type: "thinking_delta", delta: "plan" } },
      { type: "message_update", eventId: "msg-2", assistantMessageEvent: { type: "text_delta", delta: "answer" } },
      { type: "tool_execution_start", toolCallId: "call-1", toolName: "load_marketing_skill", args: { token: "secret" } },
      { type: "tool_execution_end", toolCallId: "call-1", isError: true, result: { password: "secret" } },
    ].flatMap((event) => projector.project(event));

    expect(events.map((event) => event.event_type)).toEqual([
      "agent.turn.start", "turn.start", "thinking.delta", "message.delta", "tool.start", "tool.end",
    ]);
    expect(events[4]?.payload).toEqual({ call_id: "call-1", internal_tool_name: "load_marketing_skill" });
    expect(events[5]?.payload).toEqual({ call_id: "call-1", status: "failed" });
    expect(JSON.stringify(events)).not.toContain("secret");
  });

  it("emits message.completed before usage when done carries both text and usage", () => {
    const projector = new PiSdkUsageProjector("attempt-mix");
    const events = [
      {
        type: "message_update",
        eventId: "msg-done-1",
        assistantMessageEvent: {
          type: "done",
          message: {
            role: "assistant",
            content: [{ type: "text", text: "最终结论" }],
            usage: { input: 12, output: 4, requestId: "req-mix" },
          },
        },
      },
      { type: "agent_end" },
    ].flatMap((event) => projector.project(event));

    // usage 随 done 帧投影；completion 推迟到 agent_end 发布且先于 turn.end，
    // 保证 terminal 前持久化的顺序不变。
    expect(events.map((event) => event.event_type)).toEqual(["usage", "message.completed", "agent.turn.end"]);
    expect(events[1]).toMatchObject({
      event_type: "message.completed",
      payload: { text: "最终结论" },
    });
    expect(events[0]).toMatchObject({
      event_type: "usage",
      payload: { input_tokens: 12, output_tokens: 4, usage_status: "available" },
    });
  });

  it("emits message.completed for done even without usage, and only once per attempt", () => {
    const projector = new PiSdkUsageProjector("attempt-once");
    const first = projector.project({
      type: "message_update",
      assistantMessageEvent: { type: "done", message: { role: "assistant", content: [{ type: "text", text: "结论" }] } },
    });
    const second = projector.project({
      type: "message_update",
      assistantMessageEvent: { type: "done", message: { role: "assistant", content: [{ type: "text", text: "结论" }] } },
    });
    const boundary = projector.project({ type: "agent_end" });

    // 无 usage 的 done 帧不再立即制造 unavailable；unavailable 由 turn 边界
    // 兜底且恰好一条。completion 推迟到 agent_end 且每 Attempt 恰好一次。
    expect(first.filter((event) => event.event_type === "usage")).toEqual([]);
    expect(second.filter((event) => event.event_type === "usage")).toEqual([]);
    expect(boundary.map((event) => event.event_type)).toEqual(["message.completed", "agent.turn.end", "usage"]);
    expect(boundary[0]?.payload).toEqual({ text: "结论" });
    expect(boundary[2]?.payload).toEqual({ usage_status: "unavailable" });
    expect(projector.project({ type: "agent_end" }).map((event) => event.event_type)).toEqual(
      ["agent.turn.end"],
    );
  });

  it("never lets a usage payload swallow the assistant completion", () => {
    const projector = new PiSdkUsageProjector("attempt-terminal");
    const projected = [
      {
        type: "message_update",
        assistantMessageEvent: { type: "text_delta", delta: "部分" },
      },
      {
        type: "message_update",
        assistantMessageEvent: {
          type: "done",
          message: { role: "assistant", content: [{ type: "text", text: "部分结论" }], usage: { input: 1 } },
        },
      },
      { type: "agent_end" },
    ].flatMap((event) => projector.project(event));

    const types = projected.map((event) => event.event_type);
    expect(types).toEqual(["message.delta", "usage", "message.completed", "agent.turn.end"]);
    expect(types.indexOf("message.completed")).toBeLessThan(types.indexOf("agent.turn.end"));
    expect(projected[2]?.payload).toEqual({ text: "部分结论" });
  });

  it("projects provider usage with cache fields and stable source ids", () => {
    const projector = new PiSdkUsageProjector("attempt-1");
    const events = projector.project({
      type: "message_update",
      message: {
        usage: {
          input: 120,
          output: 30,
          cacheRead: 10,
          cacheWrite: 2,
          requestId: "req-1",
        },
      },
    });

    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({
      source_event_id: "attempt-1:1",
      sequence: 1,
      event_type: "usage",
      payload: {
        input_tokens: 120,
        output_tokens: 30,
        cache_read_tokens: 10,
        cache_write_tokens: 2,
        upstream_request_id: "req-1",
        usage_status: "available",
      },
    });
  });

  it("records one unavailable per usage-less turn at the turn boundary and ignores duplicates/unknown events", () => {
    const projector = new PiSdkUsageProjector("attempt-2");
    // 同一 turn 内：无 usage 的 message_end 不再立即产 unavailable；turn_end 兜底恰好一条。
    const deferred = projector.project({ eventId: "end-1", type: "message_end", message: { role: "assistant" } });
    const fallback = projector.project({ eventId: "end-2", type: "turn_end", message: { role: "assistant" } });
    const dupMessage = projector.project({ eventId: "end-1", type: "message_end", message: { role: "assistant" } });
    const dupBoundary = projector.project({ eventId: "end-3", type: "turn_end", message: { role: "assistant" } });
    const unknown = projector.project({ type: "queue_update", steering: [] });

    expect(deferred.filter((event) => event.event_type === "usage")).toEqual([]);
    const fallbackUsage = fallback.filter((event) => event.event_type === "usage");
    expect(fallbackUsage).toHaveLength(1);
    expect(fallbackUsage[0]?.payload).toEqual({ usage_status: "unavailable" });
    expect(dupMessage.filter((event) => event.event_type === "usage")).toEqual([]);
    expect(dupBoundary.filter((event) => event.event_type === "usage")).toEqual([]);
    expect(unknown).toEqual([]);
    expect(projector.diagnostics.unknownEvents).toBe(1);
  });

  it("keeps distinct unavailable calls in distinct turns without an SDK identity", () => {
    const projector = new PiSdkUsageProjector("attempt-2b");

    projector.project({ type: "turn_start" });
    projector.project({ type: "message_end", message: { role: "assistant" } });
    const firstTurn = projector.project({ type: "turn_end", message: { role: "assistant" } });
    projector.project({ type: "turn_start" });
    projector.project({ type: "message_end", message: { role: "assistant" } });
    const secondTurn = projector.project({ type: "turn_end", message: { role: "assistant" } });

    const first = firstTurn.filter((event) => event.event_type === "usage");
    const second = secondTurn.filter((event) => event.event_type === "usage");
    expect(first).toHaveLength(1);
    expect(second).toHaveLength(1);
    expect(first[0]?.payload).toEqual({ usage_status: "unavailable" });
    expect(second[0]?.payload).toEqual({ usage_status: "unavailable" });
    expect(projector.diagnostics.duplicateUsage).toBe(0);
  });

  it("projects only safe usage fields and suppresses duplicate request ids", () => {
    const projector = new PiSdkUsageProjector("attempt-3");
    const first = projectPiSdkEvent(
      { type: "usage", usage: { input: 1, output: 2, requestId: "req-3", token: "secret" } },
      projector,
    );
    const duplicate = projectPiSdkEvent(
      { type: "usage", usage: { input: 1, output: 2, requestId: "req-3", token: "secret" } },
      projector,
    );

    expect(first[0]?.payload).toEqual({
      input_tokens: 1,
      output_tokens: 2,
      upstream_request_id: "req-3",
      usage_status: "available",
    });
    expect(first[0]?.payload).not.toHaveProperty("token");
    expect(duplicate).toEqual([]);
  });

  it("every projector output passes the control-plane source-event wire contract", () => {
    // 回归：projector 新增别名（如 turn.start）必须同步进入 protocol 白名单，
    // 否则 sendEvent 在 flush 时同步抛错，事件流被当作控制面不可用而中断。
    const projector = new PiSdkUsageProjector("attempt-wire");
    const sdkEvents: unknown[] = [
      { type: "agent_start" },
      { type: "turn_start" },
      { type: "message_start" },
      { type: "message_update", assistantMessageEvent: { type: "thinking_delta", delta: "思考" } },
      { type: "message_update", assistantMessageEvent: { type: "text_delta", delta: "片段" } },
      { type: "tool_execution_start", toolCallId: "call-1", toolName: "mcp" },
      { type: "tool_execution_end", toolCallId: "call-1", isError: false },
      {
        type: "message_update",
        assistantMessageEvent: {
          type: "text_end",
          content: "结论",
          message: { usage: { input: 5, output: 2, requestId: "req-wire" } },
        },
      },
      { type: "turn_end" },
    ];
    const projected = sdkEvents.flatMap((event) => projector.project(event));
    expect(projected.length).toBeGreaterThanOrEqual(9);
    for (const event of projected) {
      expect(() => parsePiGatewaySourceEvent(event)).not.toThrow();
    }
  });

  it("emits message.completed only for the final assistant text at agent_end", () => {
    // 文本前导 → 工具调用 → 最终回答：前导 text_end 不得产出 completion；
    // 只有 agent_end/turn_end 收口时的最近一段最终文本才是 completion。
    const projector = new PiSdkUsageProjector("attempt-final");
    const events = [
      { type: "agent_start" },
      { type: "turn_start" },
      { type: "message_update", assistantMessageEvent: { type: "text_end", content: "我先查一下数据。" } },
      { type: "tool_execution_start", toolCallId: "call-1", toolName: "mcp" },
      { type: "tool_execution_end", toolCallId: "call-1", isError: false },
      { type: "turn_end" },
      { type: "turn_start" },
      { type: "message_update", assistantMessageEvent: { type: "text_end", content: "最终结论：声量上升。" } },
      { type: "turn_end" },
      { type: "agent_end" },
    ].flatMap((event) => projector.project(event));

    const types = events.map((event) => event.event_type);
    const completions = events.filter((event) => event.event_type === "message.completed");
    expect(completions).toHaveLength(1);
    expect(completions[0]?.payload).toEqual({ text: "最终结论：声量上升。" });
    // completion 必须在最后一个 turn.end 之前、且不出现在前导语之后
    expect(types.indexOf("message.completed")).toBeGreaterThan(types.lastIndexOf("tool.end"));
    expect(types.filter((t) => t === "message.completed")).toHaveLength(1);
  });

  it("does not emit a completion when a text preamble is followed only by tool calls", () => {
    const projector = new PiSdkUsageProjector("attempt-preamble");
    const events = [
      { type: "message_update", assistantMessageEvent: { type: "text_end", content: "准备调用工具" } },
      { type: "tool_execution_start", toolCallId: "call-1", toolName: "mcp" },
      { type: "tool_execution_end", toolCallId: "call-1", isError: false },
      { type: "agent_end" },
    ].flatMap((event) => projector.project(event));

    expect(events.filter((event) => event.event_type === "message.completed")).toEqual([]);
  });

  it("sends usage through the authenticated control-plane event path", async () => {
    const calls: RequestInit[] = [];
    const client = new ControlPlaneClient({
      origin: "http://127.0.0.1:8000",
      environment: "test",
      gatewayId: "gateway-1",
      internalSecret: "secret",
      nonceFactory: () => "nonce-usage",
      timestamp: () => 1_700_000_000,
      fetchImpl: async (_url, init) => {
        calls.push(init ?? {});
        return new Response(JSON.stringify({ ok: true }), { status: 200 });
      },
    });
    const projector = new PiSdkUsageProjector("attempt-4");
    const [event] = projector.project({ type: "usage", usage: { input: 3 } });
    await client.sendUsage("run-1", event!, "lease-token-012345678901234567890123");

    expect(calls).toHaveLength(1);
    expect(JSON.parse(String(calls[0].body))).toMatchObject({ event_type: "usage" });
    expect(calls[0].headers).toMatchObject({ "X-Pi-Run-Lease": "lease-token-012345678901234567890123" });
  });
});

describe("usage dedup per provider call (real event shapes)", () => {
  const USAGE = { input: 10, output: 5 };

  it("done with usage followed by turn_end with the same usage yields exactly one record", () => {
    const projector = new PiSdkUsageProjector("attempt-d1");
    const first = projector.project({
      type: "message_update",
      assistantMessageEvent: { type: "done", message: { id: "m1", role: "assistant", usage: USAGE } },
      message: { id: "m1", role: "assistant", usage: USAGE },
    });
    const second = projector.project({ type: "turn_end", message: { id: "m1", role: "assistant", usage: USAGE } });
    const usageEvents = [...first, ...second].filter((e) => e.event_type === "usage");
    expect(usageEvents).toHaveLength(1);
    expect(usageEvents[0]?.payload).toMatchObject({ input_tokens: 10, output_tokens: 5, usage_status: "available" });
  });

  it("dedups by upstream request id when present", () => {
    const projector = new PiSdkUsageProjector("attempt-d2");
    const usage = { input: 10, output: 5, requestId: "req-9" };
    projector.project({ type: "message_update", assistantMessageEvent: { type: "done", message: { usage } }, message: { usage } });
    const second = projector.project({ type: "turn_end", message: { usage } });
    expect(second.filter((e) => e.event_type === "usage")).toEqual([]);
  });

  it("message_update without usage emits nothing; a later turn_end supplies the real usage", () => {
    const projector = new PiSdkUsageProjector("attempt-d3");
    const first = projector.project({
      type: "message_update",
      assistantMessageEvent: { type: "done", message: { id: "m2", role: "assistant" } },
      message: { id: "m2", role: "assistant" },
    });
    expect(first.filter((e) => e.event_type === "usage")).toEqual([]);
    const second = projector.project({ type: "turn_end", message: { id: "m2", role: "assistant", usage: USAGE } });
    const usageEvents = second.filter((e) => e.event_type === "usage");
    expect(usageEvents).toHaveLength(1);
    expect(usageEvents[0]?.payload).toMatchObject({ usage_status: "available", input_tokens: 10 });
  });

  it("identical token counts in different turns are never deduped across turns", () => {
    const projector = new PiSdkUsageProjector("attempt-d4");
    projector.project({ type: "turn_start" });
    const a1 = projector.project({ type: "message_update", assistantMessageEvent: { type: "done", message: { usage: USAGE } }, message: { usage: USAGE } });
    const a2 = projector.project({ type: "turn_end", message: { usage: USAGE } });
    projector.project({ type: "turn_start" });
    const b1 = projector.project({ type: "message_update", assistantMessageEvent: { type: "done", message: { usage: USAGE } }, message: { usage: USAGE } });
    const b2 = projector.project({ type: "turn_end", message: { usage: USAGE } });
    const all = [...a1, ...a2, ...b1, ...b2].filter((e) => e.event_type === "usage");
    expect(all).toHaveLength(2);
  });

  it("a fully usage-less turn yields exactly one unavailable record", () => {
    const projector = new PiSdkUsageProjector("attempt-d5");
    projector.project({ type: "turn_start" });
    const first = projector.project({
      type: "message_update",
      assistantMessageEvent: { type: "done", message: { id: "m3", role: "assistant" } },
      message: { id: "m3", role: "assistant" },
    });
    const second = projector.project({ type: "message_end", message: { id: "m3", role: "assistant" } });
    const third = projector.project({ type: "turn_end", message: { id: "m3", role: "assistant" } });
    const all = [...first, ...second, ...third].filter((e) => e.event_type === "usage");
    expect(all).toHaveLength(1);
    expect(all[0]?.payload).toEqual({ usage_status: "unavailable" });
  });
});
