from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Protocol, TypeAlias

from app.mcp_gateway.contracts import DataTapService


JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
McpResultStatus: TypeAlias = Literal["available", "empty", "unavailable"]
MCP_UNAVAILABLE_REASONS = frozenset(
    {
        "payload_too_large",
        "payload_not_retrievable",
        "invalid_json_text",
        "unsupported_content",
        "local_persistence_failed",
    }
)


@dataclass(frozen=True)
class DiscoveredTool:
    name: str
    description: str | None
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None


@dataclass(frozen=True)
class RemoteToolResult:
    structured_content: JsonValue
    is_error: bool
    upstream_request_id: str | None
    # 上游业务错误的原文（截断、脱敏前的原始文本），用于回喂模型自我纠正。
    error_text: str | None = None
    # DataTap adapter 只有在能观察到真实响应形状时才填写；legacy fixture 未填写
    # 时由消费方按 structured_content 推导，避免把旧 transport mock 误判为 unknown。
    result_status: McpResultStatus | None = None
    unavailable_reason: str | None = None


def is_non_empty_json(value: Any) -> bool:
    """Return whether a value can be trusted as non-empty JSON content."""

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


def contains_transport_artifact_marker(value: Any) -> bool:
    """Reject adapter transport metadata as an Evidence payload."""

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
            if contains_transport_artifact_marker(item):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(contains_transport_artifact_marker(item) for item in value)
    return False


def resolve_remote_result_status(
    result: RemoteToolResult,
) -> tuple[McpResultStatus | None, str | None]:
    """Resolve and validate the strict three-state result contract.

    Older in-process fixtures omitted ``result_status``. They are only inferred
    from the payload for compatibility; adapter-produced results must carry an
    explicit status and reason where required. Any explicit contradiction is
    rejected before output-schema validation or accounting.
    """

    status = result.result_status
    if status is None:
        if result.unavailable_reason is not None:
            return None, "mcp_result_status_mismatch"
        if not is_non_empty_json(result.structured_content):
            return "empty", None
        if contains_transport_artifact_marker(result.structured_content):
            return None, "mcp_result_status_mismatch"
        return "available", None
    if status == "available":
        if (
            not is_non_empty_json(result.structured_content)
            or contains_transport_artifact_marker(result.structured_content)
            or result.unavailable_reason is not None
        ):
            return None, "mcp_result_status_mismatch"
        return status, None
    if status == "empty":
        if result.structured_content is not None or result.unavailable_reason is not None:
            return None, "mcp_result_status_mismatch"
        return status, None
    if status == "unavailable":
        if (
            result.structured_content is not None
            or result.unavailable_reason not in MCP_UNAVAILABLE_REASONS
        ):
            return None, "mcp_result_status_mismatch"
        return status, None
    return None, "mcp_result_status_mismatch"


class McpTransport(Protocol):
    def protocol_session_digest(self, service: DataTapService) -> str | None: ...

    async def list_tools(self, service: DataTapService) -> tuple[DiscoveredTool, ...]: ...

    async def call_tool(
        self,
        service: DataTapService,
        remote_name: str,
        arguments: Mapping[str, JsonValue],
    ) -> RemoteToolResult: ...

    async def reconcile_tool_call(self, upstream_request_id: str) -> RemoteToolResult | None: ...


@dataclass(frozen=True)
class ToolInvocationOutcome:
    status: Literal["succeeded", "failed", "unknown"]
    validated_output: JsonValue | None
    response_hash: str | None
    upstream_request_id: str | None
    error_type: str | None
    safe_diagnostic: dict[str, JsonValue] | None = None
    # 上游业务错误原文（已脱敏截断），供记账持久化并回喂模型。
    error_message: str | None = None
    result_status: McpResultStatus | None = None


class ServiceNotAllowedError(ValueError):
    pass


class PossiblySentTimeout(TimeoutError):
    pass


class McpUpstreamError(RuntimeError):
    pass


class McpNotSentError(McpUpstreamError):
    """The call failed before the transport could dispatch a tool request."""


class McpConnectionTimeout(McpUpstreamError):
    """The MCP endpoint could not be connected before a request was sent."""


class McpConnectionError(McpUpstreamError):
    """The MCP endpoint connection failed before a request was sent."""


class McpProtocolError(McpUpstreamError):
    """The MCP transport or protocol response was malformed."""


class McpGatewayTimeout(McpUpstreamError):
    """The MCP gateway returned an HTTP timeout such as 504."""


class McpUpstreamHttpError(McpUpstreamError):
    """The MCP gateway returned an unexpected HTTP error response."""


class McpQueueTimeout(McpUpstreamError):
    """The per-service concurrency queue could not admit the call in time."""


class McpCircuitOpen(McpUpstreamError):
    """The per-service circuit breaker is open or its probe is busy."""


class LogicalCallConflictError(ValueError):
    pass
