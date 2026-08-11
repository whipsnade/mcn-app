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

    // done 帧只投影 usage；completion 推迟到 agent_end 且每 Attempt 恰好一次
    expect(first.map((event) => event.event_type)).toEqual(["usage"]);
    expect(first[0]?.payload).toMatchObject({ usage_status: "unavailable" });
    expect(second.map((event) => event.event_type)).toEqual(["usage"]);
    expect(boundary.map((event) => event.event_type)).toEqual(["message.completed", "agent.turn.end"]);
    expect(boundary[0]?.payload).toEqual({ text: "结论" });
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

  it("records unavailable without estimating missing usage and ignores duplicates/unknown events", () => {
    const projector = new PiSdkUsageProjector("attempt-2");
    const first = projector.project({ eventId: "end-1", type: "message_end", message: { role: "assistant" } });
    const duplicate = projector.project({ eventId: "end-1", type: "message_end", message: { role: "assistant" } });
    const unknown = projector.project({ type: "queue_update", steering: [] });

    expect(first[0]).toMatchObject({
      source_event_id: "attempt-2:1",
      payload: { usage_status: "unavailable" },
    });
    expect(first[0]?.payload).not.toHaveProperty("input_tokens");
    expect(duplicate).toEqual([]);
    expect(unknown).toEqual([]);
    expect(projector.diagnostics.unknownEvents).toBe(1);
  });

  it("keeps distinct unavailable calls without an SDK identity", () => {
    const projector = new PiSdkUsageProjector("attempt-2b");

    const first = projector.project({ type: "message_end", message: { role: "assistant" } });
    const second = projector.project({ type: "message_end", message: { role: "assistant" } });

    expect(first[0]?.source_event_id).toBe("attempt-2b:1");
    expect(second[0]?.source_event_id).toBe("attempt-2b:2");
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
