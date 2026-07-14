from __future__ import annotations

import pytest

from app.mcp_gateway.validation import (
    McpValidationError,
    ValidationLimits,
    validate_input,
    validate_output,
)

from tests.mcp_gateway.fakes import strict_object_schema


@pytest.mark.parametrize(
    ("arguments", "limits"),
    [
        ({"value": "x" * 9}, ValidationLimits(max_string_length=8)),
        ({"value": [1, 2, 3]}, ValidationLimits(max_array_items=2)),
        ({"value": {"nested": {"too": "deep"}}}, ValidationLimits(max_depth=2)),
        ({"value": 101}, ValidationLimits(max_abs_number=100)),
        ({"value": "x" * 20}, ValidationLimits(max_bytes=10)),
    ],
)
def test_input_limits_reject_oversized_or_deep_values(arguments, limits) -> None:
    schema = strict_object_schema({"value": {}})
    with pytest.raises(McpValidationError):
        validate_input(arguments, schema, limits=limits)


@pytest.mark.parametrize(
    "arguments",
    [
        {"url": "https://example.com"},
        {"host": "example.com"},
        {"authorization": "secret"},
        {"service_slug": "bilibili-mcp"},
        {"nested": {"device_id": "abc"}},
    ],
)
def test_input_rejects_routing_fields_at_any_depth(arguments) -> None:
    with pytest.raises(McpValidationError):
        validate_input(arguments, strict_object_schema({}))


def test_schema_requires_additional_properties_false() -> None:
    with pytest.raises(McpValidationError):
        validate_input({}, {"type": "object", "properties": {}})


def test_input_and_output_reject_unknown_fields_and_wrong_types() -> None:
    schema = strict_object_schema({"count": {"type": "integer"}}, "count")
    with pytest.raises(McpValidationError):
        validate_input({"count": 1, "extra": True}, schema)
    with pytest.raises(McpValidationError):
        validate_output({"count": "1"}, schema)


def test_valid_input_and_output_are_returned_unchanged() -> None:
    schema = strict_object_schema({"count": {"type": "integer"}}, "count")
    assert validate_input({"count": 1}, schema) == {"count": 1}
    assert validate_output({"count": 1}, schema) == {"count": 1}


def test_output_may_contain_url_as_untrusted_evidence_data() -> None:
    schema = strict_object_schema({"url": {"type": "string"}}, "url")
    evidence = {"url": "https://example.invalid/evidence"}
    assert validate_output(evidence, schema) == evidence
