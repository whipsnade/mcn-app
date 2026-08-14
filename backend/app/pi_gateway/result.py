"""Legacy/current-runtime MCP wrapper checks.

The Pi production path intentionally does not import this module.  These
helpers remain only because the legacy ``AgentMcpTool`` and MCP gateway have a
separate, compatibility execution path whose reviewed wrapper contract still
needs to be enforced.  They do not define a Pi control-plane envelope and do
not create Evidence.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


WRAPPED_RESULT_SERVICES = frozenset({"insight-cube-mcp", "social-grow-mcp"})


def _contains_transport_artifact_marker(value: Any) -> bool:
    """Reject adapter transport metadata in the legacy reviewed wrapper."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in {
                "fullResultPath",
                "full_result_path",
                "resultWriteError",
                "resource",
                "image",
                "audio",
                "summary",
                "omitted",
            }:
                return True
            if (
                str(key).lower() in {"path", "filepath", "file_path", "temppath", "temp_path"}
                and isinstance(item, str)
                and item.startswith(("/tmp/", "/private/tmp/", "/var/tmp/", "/var/folders/"))
            ):
                return True
            if _contains_transport_artifact_marker(item):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_contains_transport_artifact_marker(item) for item in value)
    return False


def validate_wrapped_result_json(service: str, value: Any) -> bool:
    """Validate DataTap's reviewed legacy ``{result: JSON text}`` wrapper."""

    if service not in WRAPPED_RESULT_SERVICES:
        return True
    if not isinstance(value, Mapping) or not isinstance(value.get("result"), str):
        return False
    try:
        parsed = json.loads(value["result"])
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return not _contains_transport_artifact_marker(parsed)


def requires_wrapped_result_json(service: str, output_schema: Mapping[str, Any]) -> bool:
    """Return whether a legacy reviewed output schema uses the wrapper."""

    if service not in WRAPPED_RESULT_SERVICES or not isinstance(output_schema, Mapping):
        return False
    properties = output_schema.get("properties")
    required = output_schema.get("required")
    result_schema = properties.get("result") if isinstance(properties, Mapping) else None
    return (
        output_schema.get("type") == "object"
        and output_schema.get("additionalProperties") is False
        and isinstance(properties, Mapping)
        and set(properties) == {"result"}
        and isinstance(required, list)
        and set(required) == {"result"}
        and isinstance(result_schema, Mapping)
        and result_schema.get("type") == "string"
        and set(result_schema) == {"type"}
    )


def validate_reviewed_result_json(
    service: str, output_schema: Mapping[str, Any], value: Any
) -> bool:
    """Apply the legacy adapter policy attached to an audited output schema."""

    if not requires_wrapped_result_json(service, output_schema):
        return True
    return validate_wrapped_result_json(service, value)


__all__ = [
    "requires_wrapped_result_json",
    "validate_reviewed_result_json",
    "validate_wrapped_result_json",
]
