from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

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
    max_nodes: int = 10_000
    max_total_string_length: int = 262_144
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
    except (RecursionError, TypeError, ValueError) as exc:
        raise McpValidationError("value must be canonical JSON") from exc
    return encoded.encode("utf-8")


def validate_schema_policy(schema: dict[str, Any], *, reject_routing_fields: bool = True) -> None:
    if not isinstance(schema, dict):
        raise McpValidationError("schema must be an object")
    stack: list[tuple[str, Any]] = [("enter", schema)]
    active_containers: set[int] = set()
    while stack:
        action, node = stack.pop()
        if action == "leave":
            active_containers.remove(id(node))
            continue
        if not isinstance(node, (dict, list)):
            continue
        identity = id(node)
        if identity in active_containers:
            raise McpValidationError("schema must not contain a cycle")
        active_containers.add(identity)
        stack.append(("leave", node))
        if isinstance(node, list):
            stack.extend(("enter", child) for child in reversed(node))
            continue

        is_object = node.get("type") == "object" or "properties" in node
        if is_object and node.get("additionalProperties") is not False:
            raise McpValidationError("object schemas must reject additional properties")
        for keyword in ("$ref", "$dynamicRef"):
            reference = node.get(keyword)
            if isinstance(reference, str) and not reference.startswith("#"):
                raise McpValidationError("external schema references are forbidden")
        properties = node.get("properties")
        if isinstance(properties, dict) and reject_routing_fields:
            for key in properties:
                if _normalized_key(key) in _ROUTING_FIELDS:
                    raise McpValidationError("routing fields are forbidden by schema")
        stack.extend(("enter", child) for child in reversed(tuple(node.values())))

    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise McpValidationError("schema is not valid Draft 2020-12 JSON Schema") from exc


def validate_input(
    value: dict[str, JsonValue],
    schema: dict[str, Any],
    *,
    limits: ValidationLimits = DEFAULT_INPUT_LIMITS,
) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise McpValidationError("tool arguments must be an object")
    validate_schema_policy(schema, reject_routing_fields=True)
    _validate_value(value, limits=limits)
    _reject_routing_fields(value)
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
    _validate_value(value, limits=limits)
    _validate_size(value, limits)
    _validate_against_schema(value, schema, path="$")
    return value


def _normalized_key(key: str) -> str:
    snake_case = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    return re.sub(r"[^a-z0-9]+", "_", snake_case.casefold()).strip("_")


def _reject_routing_fields(value: JsonValue) -> None:
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, child in node.items():
                if _normalized_key(key) in _ROUTING_FIELDS:
                    raise McpValidationError("routing fields are forbidden")
                stack.append(child)
        elif isinstance(node, list):
            stack.extend(node)


def _validate_size(value: JsonValue | dict[str, Any], limits: ValidationLimits) -> None:
    if len(canonical_json_bytes(value)) > limits.max_bytes:
        raise McpValidationError("JSON payload exceeds byte limit")


def _validate_value(value: Any, *, limits: ValidationLimits) -> None:
    stack: list[tuple[str, Any, int]] = [("enter", value, 1)]
    active_containers: set[int] = set()
    node_count = 0
    total_string_length = 0
    while stack:
        action, node, depth = stack.pop()
        if action == "leave":
            active_containers.remove(id(node))
            continue
        if depth > limits.max_depth:
            raise McpValidationError("JSON payload exceeds nesting limit")
        node_count += 1
        if node_count > limits.max_nodes:
            raise McpValidationError("JSON payload exceeds node limit")
        if node is None or isinstance(node, bool):
            continue
        if isinstance(node, str):
            if len(node) > limits.max_string_length:
                raise McpValidationError("string exceeds length limit")
            total_string_length += len(node)
        elif isinstance(node, (int, float)):
            if isinstance(node, float) and not math.isfinite(node):
                raise McpValidationError("number must be finite")
            if abs(node) > limits.max_abs_number:
                raise McpValidationError("number exceeds range limit")
        elif isinstance(node, (dict, list)):
            identity = id(node)
            if identity in active_containers:
                raise McpValidationError("JSON payload must not contain a cycle")
            active_containers.add(identity)
            stack.append(("leave", node, depth))
            if isinstance(node, list):
                if len(node) > limits.max_array_items:
                    raise McpValidationError("array exceeds item limit")
                stack.extend(("enter", child, depth + 1) for child in reversed(node))
            else:
                if len(node) > limits.max_object_properties:
                    raise McpValidationError("object exceeds property limit")
                for key in node:
                    if not isinstance(key, str):
                        raise McpValidationError("JSON object keys must be strings")
                    if len(key) > limits.max_string_length:
                        raise McpValidationError("object key exceeds length limit")
                    total_string_length += len(key)
                stack.extend(
                    ("enter", child, depth + 1) for child in reversed(tuple(node.values()))
                )
        else:
            raise McpValidationError("value is not valid JSON")
        if total_string_length > limits.max_total_string_length:
            raise McpValidationError("JSON payload exceeds aggregate string limit")


def _validate_against_schema(value: Any, schema: dict[str, Any], *, path: str) -> None:
    try:
        Draft202012Validator(
            schema,
            format_checker=Draft202012Validator.FORMAT_CHECKER,
        ).validate(value)
    except ValidationError as exc:
        raise McpValidationError("value does not match approved schema") from exc
    except Exception as exc:
        # External references have already been rejected; remaining resolver and
        # implementation failures must still fail closed without exposing values.
        raise McpValidationError("schema validation could not be completed") from exc
