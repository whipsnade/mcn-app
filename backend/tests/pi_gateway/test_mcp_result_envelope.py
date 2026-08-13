from __future__ import annotations

import pytest

from app.pi_gateway.result import McpResultEnvelopeError, parse_mcp_result_details
from app.pi_gateway.result import validate_reviewed_result_json, validate_wrapped_result_json


def test_available_envelope_requires_non_empty_structured_content() -> None:
    parsed = parse_mcp_result_details(
        {
            "mode": "mcpResult",
            "mcpResult": {
                "envelope": "mcp_result_v1",
                "result_status": "available",
                "structuredContent": {"result": '{"rows":[]}'},
                "upstream_request_id": "upstream-redacted-1",
            },
        }
    )
    assert parsed.result_status == "available"
    assert parsed.structured_content == {"result": '{"rows":[]}' }
    assert parsed.upstream_request_id == "upstream-redacted-1"

    for value in (None, {}, [], ""):
        with pytest.raises(McpResultEnvelopeError, match="mcp_result_envelope_invalid"):
            parse_mcp_result_details(
                {
                    "mode": "mcpResult",
                    "mcpResult": {
                        "envelope": "mcp_result_v1",
                        "result_status": "available",
                        "structuredContent": value,
                    },
                }
            )


def test_empty_and_unavailable_are_disjoint_from_structured_content() -> None:
    empty = parse_mcp_result_details(
        {
            "mode": "mcpResult",
            "mcpResult": {"envelope": "mcp_result_v1", "result_status": "empty"},
        }
    )
    assert empty.result_status == "empty"
    assert empty.structured_content is None

    unavailable = parse_mcp_result_details(
        {
            "mode": "mcpResult",
            "mcpResult": {
                "envelope": "mcp_result_v1",
                "result_status": "unavailable",
                "unavailable_reason": "payload_too_large",
            },
        }
    )
    assert unavailable.result_status == "unavailable"
    assert unavailable.unavailable_reason == "payload_too_large"

    for status in ("empty", "unavailable"):
        payload = {
            "envelope": "mcp_result_v1",
            "result_status": status,
            "structuredContent": {"result": "should-not-be-here"},
        }
        if status == "unavailable":
            payload["unavailable_reason"] = "unsupported_content"
        with pytest.raises(McpResultEnvelopeError, match="mcp_result_envelope_invalid"):
            parse_mcp_result_details({"mode": "mcpResult", "mcpResult": payload})


def test_unavailable_reason_is_stable_and_unknown_dispatch_is_not_an_envelope() -> None:
    for reason in ("payload_too_large", "payload_not_retrievable", "invalid_json_text", "unsupported_content", "local_persistence_failed"):
        parsed = parse_mcp_result_details(
            {
                "mode": "mcpResult",
                "mcpResult": {
                    "envelope": "mcp_result_v1",
                    "result_status": "unavailable",
                    "unavailable_reason": reason,
                },
            }
        )
        assert parsed.unavailable_reason == reason

    with pytest.raises(McpResultEnvelopeError, match="mcp_result_envelope_invalid"):
        parse_mcp_result_details({"mode": "error", "classification": "result_unknown"})


def test_reviewed_wrapper_rejects_nested_transport_artifacts() -> None:
    assert validate_wrapped_result_json(
        "insight-cube-mcp",
        {"result": '{"rows":[{"value":1}]}'},
    )
    for marker in ("fullResultPath", "resource", "image", "audio", "summary", "omitted"):
        assert not validate_wrapped_result_json(
            "insight-cube-mcp",
            {"result": '{"rows":[{"%s":"/tmp/opaque"}]}' % marker},
        )


def test_wrapper_policy_is_bound_to_the_reviewed_schema_not_service_name_only() -> None:
    wrapped_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"result": {"type": "string"}},
        "required": ["result"],
    }
    direct_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"rows": {"type": "array"}},
    }
    assert not validate_reviewed_result_json(
        "insight-cube-mcp", wrapped_schema, {"result": "not-json"}
    )
    assert validate_reviewed_result_json(
        "insight-cube-mcp", direct_schema, {"rows": []}
    )
