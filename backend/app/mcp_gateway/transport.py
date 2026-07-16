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


class ServiceNotAllowedError(ValueError):
    pass


class PossiblySentTimeout(TimeoutError):
    pass


class McpUpstreamError(RuntimeError):
    pass


class LogicalCallConflictError(ValueError):
    pass
