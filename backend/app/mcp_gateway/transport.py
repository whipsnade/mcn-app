from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Protocol, TypeAlias

from app.mcp_gateway.contracts import DataTapService


JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


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


class McpTransport(Protocol):
    def protocol_session_digest(self, service: DataTapService) -> str | None: ...

    async def list_tools(self, service: DataTapService) -> tuple[DiscoveredTool, ...]: ...

    async def call_tool(
        self,
        service: DataTapService,
        remote_name: str,
        arguments: Mapping[str, JsonValue],
    ) -> RemoteToolResult: ...


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


class ServiceNotAllowedError(ValueError):
    pass


class PossiblySentTimeout(TimeoutError):
    pass


class McpUpstreamError(RuntimeError):
    pass


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
