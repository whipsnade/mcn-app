import pytest

from app.pi_gateway.events import (
    PiGatewayEventError,
    canonical_event_type,
    normalize_source_payload,
    parse_source_event_id,
)


def test_source_identity_and_alias_projection_are_stable() -> None:
    assert parse_source_event_id("attempt-1:7") == ("attempt-1", 7)
    assert canonical_event_type("tool.end", {"status": "error"}) == "tool.failed"
    assert canonical_event_type("tool.end", {"status": "result_unknown"}) == "tool.unknown"
    assert normalize_source_payload(
        "text.delta", {"message_id": "m-1", "delta": "hello"}
    ) == {"message_id": "m-1", "delta": "hello"}


def test_tool_end_status_variants_normalize_through_the_shared_whitelist() -> None:
    """tool.succeeded/failed/unknown 细分与 tool.completed 共享字段白名单。"""
    assert normalize_source_payload("tool.end", {"call_id": "c-1", "status": "succeeded"}) == {
        "call_id": "c-1",
        "status": "succeeded",
    }
    assert normalize_source_payload("tool.end", {"call_id": "c-1", "status": "failed"}) == {
        "call_id": "c-1",
        "status": "failed",
    }
    assert canonical_event_type("tool.end", {"status": "succeeded"}) == "tool.succeeded"
    assert canonical_event_type("tool.end", {"status": "failed"}) == "tool.failed"


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("attempt-1", "pi_gateway_source_event_invalid"),
        ("attempt-1:0", "pi_gateway_source_sequence_invalid"),
        ("attempt-1:1:2", "pi_gateway_source_event_invalid"),
    ],
)
def test_source_identity_rejects_gap_prone_or_ambiguous_ids(value: str, code: str) -> None:
    with pytest.raises(PiGatewayEventError, match=code):
        parse_source_event_id(value)


def test_projection_rejects_raw_payload_and_unbounded_text() -> None:
    with pytest.raises(PiGatewayEventError, match="pi_gateway_event_field_invalid"):
        normalize_source_payload("tool.start", {"call_id": "c", "args": {"token": "x"}})
    with pytest.raises(PiGatewayEventError, match="pi_gateway_event_text_missing"):
        normalize_source_payload("message.delta", {})
    with pytest.raises(PiGatewayEventError, match="pi_gateway_event_text_invalid"):
        normalize_source_payload("message.delta", {"text": "x" * (64 * 1024 + 1)})


def test_alias_table_stays_inside_the_wire_contract() -> None:
    """防漂移：events 别名表、contracts 请求校验与字段白名单必须同步。

    回归：projector 新增 turn.start 时只更新了别名/白名单，contracts 的
    _SOURCE_EVENT_TYPES 漏改导致事件 POST 422、flush 永久阻塞、租约过期
    双重执行。三处必须同进同退。
    """
    from app.pi_gateway.contracts import _SOURCE_EVENT_TYPES
    from app.pi_gateway.events import _EVENT_ALIASES, _SOURCE_EVENT_ALLOWED_FIELDS

    assert set(_EVENT_ALIASES) <= _SOURCE_EVENT_TYPES
    for event_type in _SOURCE_EVENT_TYPES:
        if event_type == "usage":
            continue  # usage 走 normalize_usage_payload 独立通道
        canonical = canonical_event_type(event_type)
        alias_canonical = _EVENT_ALIASES.get(event_type, event_type)
        assert (
            canonical in _SOURCE_EVENT_ALLOWED_FIELDS
            or alias_canonical in _SOURCE_EVENT_ALLOWED_FIELDS
        ), event_type
    assert canonical_event_type("turn.start") == "turn.started"
    assert normalize_source_payload("turn.start", {}) == {}
