"""Pi RPC 事件到稳定 Agent 产品事件的映射测试。"""

from app.agent_runtime.events import AgentEventType, map_pi_rpc_event


def test_thinking_delta_is_redacted_and_defaults_to_collapsed() -> None:
    mapped = map_pi_rpc_event(
        {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "thinking_delta",
                "delta": "Bearer top-secret-token",
            },
        }
    )

    assert mapped.event_type == AgentEventType.THINKING_DELTA
    assert mapped.payload["collapsed"] is True
    assert mapped.payload["text"] == "[REDACTED]"


def test_text_and_tool_events_have_stable_product_shapes_without_raw_result() -> None:
    text = map_pi_rpc_event(
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": "分析结论"},
        }
    )
    started = map_pi_rpc_event(
        {
            "type": "tool_execution_start",
            "toolCallId": "call-1",
            "toolName": "datatap_query",
            "args": {"Authorization": "Bearer secret"},
        }
    )
    ended = map_pi_rpc_event(
        {
            "type": "tool_execution_end",
            "toolCallId": "call-1",
            "toolName": "datatap_query",
            "result": {"content": [{"text": "raw supplier response"}]},
            "isError": False,
        }
    )

    assert text.event_type == AgentEventType.MESSAGE_DELTA
    assert text.payload == {"text": "分析结论"}
    assert started.event_type == AgentEventType.TOOL_STARTED
    assert started.payload == {"call_id": "call-1", "tool_name": "datatap_query"}
    assert ended.event_type == AgentEventType.TOOL_SUCCEEDED
    assert ended.payload == {"call_id": "call-1", "tool_name": "datatap_query"}


def test_unknown_or_malformed_pi_events_are_audit_only() -> None:
    assert map_pi_rpc_event({"type": "queue_update", "steering": ["x"]}) is None
    assert map_pi_rpc_event({"type": "message_update"}) is None


def test_pi_error_has_a_stable_redacted_thinking_failure_projection() -> None:
    mapped = map_pi_rpc_event(
        {"type": "error", "message": "Authorization: Bearer must-not-reach-sse"}
    )

    assert mapped.event_type == AgentEventType.THINKING_FAILED
    assert mapped.payload == {"code": "pi_rpc_error", "collapsed": True}
