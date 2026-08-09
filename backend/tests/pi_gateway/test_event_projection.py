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
