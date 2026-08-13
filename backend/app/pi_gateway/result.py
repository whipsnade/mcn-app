"""Strict server-side validation for the Pi MCP result envelope."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, Mapping


McpResultStatus = Literal["available", "empty", "unavailable"]
UnavailableReason = Literal[
    "payload_too_large",
    "payload_not_retrievable",
    "invalid_json_text",
    "unsupported_content",
    "local_persistence_failed",
]

WRAPPED_RESULT_SERVICES = frozenset({"insight-cube-mcp", "social-grow-mcp"})

_UNAVAILABLE_REASONS = frozenset(
    {
        "payload_too_large",
        "payload_not_retrievable",
        "invalid_json_text",
        "unsupported_content",
        "local_persistence_failed",
    }
)


class McpResultEnvelopeError(ValueError):
    """The Gateway did not provide a canonical, discriminated result."""

    code = "mcp_result_envelope_invalid"

    def __init__(self, message: str = code) -> None:
        super().__init__(message)


@dataclass(frozen=True)
class ParsedMcpResult:
    result_status: McpResultStatus
    structured_content: Any | None = None
    upstream_request_id: str | None = None
    unavailable_reason: UnavailableReason | None = None


def parse_mcp_result_details(details: Mapping[str, Any]) -> ParsedMcpResult:
    """Parse only the single ``mcp_result_v1`` wire shape.

    The function deliberately does not accept raw MCP content, paths, or an
    old ``structuredContent`` shortcut.  The adapter has already classified
    those at the Gateway boundary; the API remains the final trust boundary.
    """

    if not isinstance(details, Mapping) or set(details) != {"mode", "mcpResult"}:
        raise McpResultEnvelopeError()
    if details.get("mode") != "mcpResult" or not isinstance(details.get("mcpResult"), Mapping):
        raise McpResultEnvelopeError()
    envelope = details["mcpResult"]
    if envelope.get("envelope") != "mcp_result_v1":
        raise McpResultEnvelopeError()
    status = envelope.get("result_status")
    upstream_request_id = envelope.get("upstream_request_id")
    if upstream_request_id is not None and (
        not isinstance(upstream_request_id, str)
        or not upstream_request_id
        or len(upstream_request_id) > 128
    ):
        raise McpResultEnvelopeError()

    if status == "available":
        if set(envelope) - {"envelope", "result_status", "structuredContent", "upstream_request_id"}:
            raise McpResultEnvelopeError()
        if "structuredContent" not in envelope:
            raise McpResultEnvelopeError()
        structured = envelope["structuredContent"]
        if not _is_non_empty_json(structured) or _contains_transport_artifact_marker(structured):
            raise McpResultEnvelopeError()
        return ParsedMcpResult(
            result_status="available",
            structured_content=structured,
            upstream_request_id=upstream_request_id,
        )

    if status == "empty":
        if set(envelope) - {"envelope", "result_status", "upstream_request_id"}:
            raise McpResultEnvelopeError()
        return ParsedMcpResult(result_status="empty", upstream_request_id=upstream_request_id)

    if status == "unavailable":
        if set(envelope) - {
            "envelope",
            "result_status",
            "unavailable_reason",
            "upstream_request_id",
        }:
            raise McpResultEnvelopeError()
        reason = envelope.get("unavailable_reason")
        if reason not in _UNAVAILABLE_REASONS:
            raise McpResultEnvelopeError()
        return ParsedMcpResult(
            result_status="unavailable",
            upstream_request_id=upstream_request_id,
            unavailable_reason=reason,
        )

    raise McpResultEnvelopeError()


def _is_non_empty_json(value: Any) -> bool:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return False
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (dict, list, tuple)):
        return bool(value)
    return True


def _contains_transport_artifact_marker(value: Any) -> bool:
    """Reject adapter transport metadata even if a permissive schema exists."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in (
                "fullResultPath",
                "full_result_path",
                "resultWriteError",
                "resource",
                "image",
                "audio",
                "summary",
                "omitted",
            ):
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
    """Validate DataTap's reviewed ``{result: JSON text}`` wrapper."""

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
    """Return whether the reviewed allowlist contract uses DataTap's wrapper.

    The service name alone is intentionally insufficient: some internal/test
    capabilities on the same service expose a direct structured schema.  The
    wrapper policy is therefore bound to the reviewed output contract itself,
    so generic ``AgentMcpTool`` callers cannot accept malformed DataTap
    ``{result: string}`` payloads while direct-schema tools remain valid.
    """

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
    """Apply the adapter policy attached to an audited output schema."""

    if not requires_wrapped_result_json(service, output_schema):
        return True
    return validate_wrapped_result_json(service, value)


__all__ = [
    "McpResultEnvelopeError",
    "ParsedMcpResult",
    "parse_mcp_result_details",
    "requires_wrapped_result_json",
    "validate_reviewed_result_json",
    "validate_wrapped_result_json",
]
