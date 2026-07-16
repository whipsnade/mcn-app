from __future__ import annotations

import json

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


def test_output_schema_failure_exposes_paths_types_and_lengths_without_values() -> None:
    schema = strict_object_schema({"result": {"type": "string"}}, "result")
    raw = {"result": {"nickname": "不应持久化", "token": "secret"}}

    with pytest.raises(McpValidationError) as raised:
        validate_output(raw, schema)

    diagnostic = raised.value.diagnostic
    assert diagnostic is not None
    assert diagnostic["error_code"] == "schema_validation_error"
    assert diagnostic["instance_path"] == "/result"
    assert diagnostic["schema_path"]
    assert diagnostic["shape"]["type"] == "object"
    serialized = json.dumps(diagnostic, ensure_ascii=False)
    assert "不应持久化" not in serialized
    assert "secret" not in serialized
    assert "token" not in serialized


def test_valid_input_and_output_are_returned_unchanged() -> None:
    schema = strict_object_schema({"count": {"type": "integer"}}, "count")
    assert validate_input({"count": 1}, schema) == {"count": 1}
    assert validate_output({"count": 1}, schema) == {"count": 1}


def test_output_may_contain_url_as_untrusted_evidence_data() -> None:
    schema = strict_object_schema({"url": {"type": "string"}}, "url")
    evidence = {"url": "https://example.invalid/evidence"}
    assert validate_output(evidence, schema) == evidence


def test_draft_2020_12_local_ref_is_enforced() -> None:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": {
            "count": {"type": "integer", "minimum": 1},
        },
        "type": "object",
        "properties": {"count": {"$ref": "#/$defs/count"}},
        "required": ["count"],
        "additionalProperties": False,
    }

    assert validate_output({"count": 1}, schema) == {"count": 1}
    with pytest.raises(McpValidationError):
        validate_output({"count": 0}, schema)


@pytest.mark.parametrize(
    "reference",
    [
        "https://example.invalid/schema.json",
        "file:///tmp/schema.json",
        "urn:example:external",
    ],
)
def test_remote_or_external_schema_references_are_rejected(reference) -> None:
    schema = {
        "type": "object",
        "properties": {"value": {"$ref": reference}},
        "additionalProperties": False,
    }

    with pytest.raises(McpValidationError, match="external schema references"):
        validate_output({"value": 1}, schema)


@pytest.mark.parametrize("field", ["baseURL", "serviceSlug", "deviceId", "apiKey"])
def test_input_rejects_camel_case_routing_fields(field) -> None:
    schema = strict_object_schema({field: {"type": "string"}}, field)
    with pytest.raises(McpValidationError, match="routing fields"):
        validate_input({field: "secret"}, schema)


def test_deep_input_fails_with_validation_error_without_python_recursion() -> None:
    value: list[object] = []
    cursor = value
    for _ in range(1_200):
        child: list[object] = []
        cursor.append(child)
        cursor = child

    with pytest.raises(McpValidationError, match="nesting limit"):
        validate_output(value, {}, limits=ValidationLimits(max_depth=20))


def test_cyclic_input_fails_with_validation_error_without_python_recursion() -> None:
    value = {}
    value["self"] = value

    with pytest.raises(McpValidationError, match="cycle"):
        validate_output(value, {})


def test_total_node_and_aggregate_string_budgets_are_enforced() -> None:
    with pytest.raises(McpValidationError, match="node limit"):
        validate_output(
            [1, 2, 3, 4],
            {},
            limits=ValidationLimits(max_nodes=4),
        )
    with pytest.raises(McpValidationError, match="aggregate string limit"):
        validate_output(
            ["abcd", "efgh"],
            {},
            limits=ValidationLimits(max_total_string_length=7),
        )
