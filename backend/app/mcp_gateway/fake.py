from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from app.mcp_gateway.contracts import DataTapService
from app.mcp_gateway.transport import (
    DiscoveredTool,
    JsonValue,
    RemoteToolResult,
)


@dataclass
class FakeMcpTransport:
    discovered_tools: dict[DataTapService, tuple[DiscoveredTool, ...]] = field(default_factory=dict)
    call_result: RemoteToolResult = field(
        default_factory=lambda: RemoteToolResult(
            structured_content={}, is_error=False, upstream_request_id=None
        )
    )
    call_error: Exception | None = None
    call_count: int = 0

    @classmethod
    def with_discovered_tool(
        cls,
        *,
        service: DataTapService,
        remote_name: str,
        input_schema: dict,
        output_schema: dict | None,
        description: str | None = None,
    ) -> "FakeMcpTransport":
        return cls(
            discovered_tools={
                service: (
                    DiscoveredTool(
                        name=remote_name,
                        description=description,
                        input_schema=input_schema,
                        output_schema=output_schema,
                    ),
                )
            }
        )

    async def list_tools(self, service: DataTapService) -> tuple[DiscoveredTool, ...]:
        if not isinstance(service, DataTapService):
            raise TypeError("service must be a DataTapService")
        return self.discovered_tools.get(service, ())

    async def call_tool(
        self,
        service: DataTapService,
        remote_name: str,
        arguments: Mapping[str, JsonValue],
    ) -> RemoteToolResult:
        if not isinstance(service, DataTapService):
            raise TypeError("service must be a DataTapService")
        self.call_count += 1
        if self.call_error is not None:
            raise self.call_error
        return self.call_result
