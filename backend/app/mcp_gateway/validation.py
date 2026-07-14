from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

from app.mcp_gateway.transport import JsonValue


class McpValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ValidationLimits:
    max_bytes: int = 65_536
    max_depth: int = 12
    max_string_length: int = 16_384
    max_array_items: int = 1_000
    max_object_properties: int = 1_000
    max_abs_number: float = 1_000_000_000_000_000


DEFAULT_INPUT_LIMITS = ValidationLimits()
DEFAULT_OUTPUT_LIMITS = ValidationLimits(max_bytes=1_048_576)

_ROUTING_FIELDS = {
    "api_key",
    "authorization",
    "authorization_header",
    "base_url",
    "credential",
    "credentials",
    "device_id",
    "endpoint",
    "headers",
    "host",
    "hostname",
    "path",
    "service",
    "service_slug",
    "token",
    "uri",
    "url",
}


def canonical_json_bytes(value: JsonValue | dict[str, Any]) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise McpValidationError("value must be canonical JSON") from exc
    return encoded.encode("utf-8")


def validate_schema_policy(schema: dict[str, Any], *, reject_routing_fields: bool = True) -> None:
    if not isinstance(schema, dict):
        raise McpValidationError("schema must be an object")

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            is_object = node.get("type") == "object" or "properties" in node
            if is_object and node.get("additionalProperties") is not False:
                raise McpValidationError("object schemas must reject additional properties")
            properties = node.get("properties")
            if isinstance(properties, dict):
                for key, child in properties.items():
                    if reject_routing_fields and _normalized_key(key) in _ROUTING_FIELDS:
                        raise McpValidationError("routing fields are forbidden by schema")
                    visit(child)
            for keyword in ("items", "additionalProperties", "not", "if", "then", "else"):
                child = node.get(keyword)
                if isinstance(child, (dict, list)):
                    visit(child)
            for keyword in ("allOf", "anyOf", "oneOf", "prefixItems"):
                children = node.get(keyword)
                if isinstance(children, list):
                    visit(children)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(schema)


def validate_input(
    value: dict[str, JsonValue],
    schema: dict[str, Any],
    *,
    limits: ValidationLimits = DEFAULT_INPUT_LIMITS,
) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise McpValidationError("tool arguments must be an object")
    _reject_routing_fields(value)
    validate_schema_policy(schema, reject_routing_fields=True)
    _validate_value(value, limits=limits, depth=1)
    _validate_size(value, limits)
    _validate_against_schema(value, schema, path="$")
    return value


def validate_output(
    value: JsonValue,
    schema: dict[str, Any],
    *,
    limits: ValidationLimits = DEFAULT_OUTPUT_LIMITS,
) -> JsonValue:
    validate_schema_policy(schema, reject_routing_fields=False)
    _validate_value(value, limits=limits, depth=1)
    _validate_size(value, limits)
    _validate_against_schema(value, schema, path="$")
    return value


def _normalized_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")


def _reject_routing_fields(value: JsonValue) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if _normalized_key(key) in _ROUTING_FIELDS:
                raise McpValidationError("routing fields are forbidden")
            _reject_routing_fields(child)
    elif isinstance(value, list):
        for child in value:
            _reject_routing_fields(child)


def _validate_size(value: JsonValue | dict[str, Any], limits: ValidationLimits) -> None:
    if len(canonical_json_bytes(value)) > limits.max_bytes:
        raise McpValidationError("JSON payload exceeds byte limit")


def _validate_value(value: Any, *, limits: ValidationLimits, depth: int) -> None:
    if depth > limits.max_depth:
        raise McpValidationError("JSON payload exceeds nesting limit")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, str):
        if len(value) > limits.max_string_length:
            raise McpValidationError("string exceeds length limit")
        return
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise McpValidationError("number must be finite")
        if abs(value) > limits.max_abs_number:
            raise McpValidationError("number exceeds range limit")
        return
    if isinstance(value, list):
        if len(value) > limits.max_array_items:
            raise McpValidationError("array exceeds item limit")
        for child in value:
            _validate_value(child, limits=limits, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > limits.max_object_properties:
            raise McpValidationError("object exceeds property limit")
        for key, child in value.items():
            if not isinstance(key, str):
                raise McpValidationError("JSON object keys must be strings")
            if len(key) > limits.max_string_length:
                raise McpValidationError("object key exceeds length limit")
            _validate_value(child, limits=limits, depth=depth + 1)
        return
    raise McpValidationError("value is not valid JSON")


def _validate_against_schema(value: Any, schema: dict[str, Any], *, path: str) -> None:
    if not schema:
        return
    if "const" in schema and value != schema["const"]:
        raise McpValidationError(f"{path} does not match const")
    if "enum" in schema and value not in schema["enum"]:
        raise McpValidationError(f"{path} is not an allowed enum value")

    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        if not any(_matches_type(value, item) for item in schema_type):
            raise McpValidationError(f"{path} has an invalid type")
    elif isinstance(schema_type, str) and not _matches_type(value, schema_type):
        raise McpValidationError(f"{path} has an invalid type")

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        for name in required:
            if name not in value:
                raise McpValidationError(f"{path}.{name} is required")
        unknown = set(value) - set(properties)
        if unknown and schema.get("additionalProperties") is False:
            raise McpValidationError(f"{path} has unknown properties")
        for name, child in value.items():
            child_schema = properties.get(name)
            if isinstance(child_schema, dict):
                _validate_against_schema(child, child_schema, path=f"{path}.{name}")
        min_properties = schema.get("minProperties")
        max_properties = schema.get("maxProperties")
        if min_properties is not None and len(value) < min_properties:
            raise McpValidationError(f"{path} has too few properties")
        if max_properties is not None and len(value) > max_properties:
            raise McpValidationError(f"{path} has too many properties")

    if isinstance(value, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, child in enumerate(value):
                _validate_against_schema(child, item_schema, path=f"{path}[{index}]")
        if schema.get("minItems") is not None and len(value) < schema["minItems"]:
            raise McpValidationError(f"{path} has too few items")
        if schema.get("maxItems") is not None and len(value) > schema["maxItems"]:
            raise McpValidationError(f"{path} has too many items")

    if isinstance(value, str):
        if schema.get("minLength") is not None and len(value) < schema["minLength"]:
            raise McpValidationError(f"{path} is too short")
        if schema.get("maxLength") is not None and len(value) > schema["maxLength"]:
            raise McpValidationError(f"{path} is too long")
        pattern = schema.get("pattern")
        if pattern is not None and re.search(pattern, value) is None:
            raise McpValidationError(f"{path} does not match pattern")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if schema.get("minimum") is not None and value < schema["minimum"]:
            raise McpValidationError(f"{path} is below minimum")
        if schema.get("maximum") is not None and value > schema["maximum"]:
            raise McpValidationError(f"{path} is above maximum")
        if schema.get("exclusiveMinimum") is not None and value <= schema["exclusiveMinimum"]:
            raise McpValidationError(f"{path} is below exclusive minimum")
        if schema.get("exclusiveMaximum") is not None and value >= schema["exclusiveMaximum"]:
            raise McpValidationError(f"{path} is above exclusive maximum")

    for keyword, require_all in (("allOf", True), ("anyOf", False), ("oneOf", False)):
        alternatives = schema.get(keyword)
        if not isinstance(alternatives, list):
            continue
        matches = 0
        for alternative in alternatives:
            try:
                _validate_against_schema(value, alternative, path=path)
            except McpValidationError:
                continue
            matches += 1
        if require_all and matches != len(alternatives):
            raise McpValidationError(f"{path} does not match all schemas")
        if keyword == "anyOf" and matches == 0:
            raise McpValidationError(f"{path} does not match any schema")
        if keyword == "oneOf" and matches != 1:
            raise McpValidationError(f"{path} does not match exactly one schema")


def _matches_type(value: Any, schema_type: str) -> bool:
    return {
        "null": value is None,
        "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "string": isinstance(value, str),
        "array": isinstance(value, list),
        "object": isinstance(value, dict),
    }.get(schema_type, False)
