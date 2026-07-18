from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from uuid import uuid4

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError
from pydantic import SecretStr

from app.mcp_gateway.contracts import DataTapService
from app.mcp_gateway.transport import (
    DiscoveredTool,
    JsonValue,
    McpCircuitOpen,
    McpConnectionError,
    McpConnectionTimeout,
    McpGatewayTimeout,
    McpProtocolError,
    McpQueueTimeout,
    McpUpstreamHttpError,
    McpUpstreamError,
    PossiblySentTimeout,
    RemoteToolResult,
    ServiceNotAllowedError,
)


_DATATAP_ORIGIN = "https://datatap.deepminer.com.cn"
_DISABLED_SERVICES = {
    "zhihu-mcp",
    "toutiao-mcp",
    "baidu-index-mcp",
    "google-trends-mcp",
}


@dataclass
class _ServiceState:
    semaphore: asyncio.Semaphore
    failures: int = 0
    opened_at: float | None = None
    half_open_in_flight: bool = False
    epoch: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class DataTapTransport:
    def __init__(
        self,
        *,
        token: SecretStr,
        gateway_session_id: str | None = None,
        credential_version: str = "v1",
        session_opener: Callable[..., AbstractAsyncContextManager[Any]] = (streamable_http_client),
        session_factory: Callable[..., AbstractAsyncContextManager[Any]] = ClientSession,
        http_transport: httpx.AsyncBaseTransport | None = None,
        # DataTap streamable HTTP gateways are service-scoped and may return
        # 504 when several long-running calls share one service endpoint.
        # Keep cross-service parallelism, but serialize calls per service.
        max_concurrency_per_service: int = 1,
        failure_threshold: int = 3,
        circuit_reset_seconds: float = 30.0,
        # Calls for one service are serialized; wait long enough for the
        # preceding long-running MCP request to finish instead of failing the
        # queued call after the old 5-second window.
        queue_timeout_seconds: float = 300.0,
        connect_timeout_seconds: float = 5.0,
        read_timeout_seconds: float = 300.0,
        write_timeout_seconds: float = 10.0,
        pool_timeout_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        secret = token.get_secret_value()
        if not secret.strip():
            raise ValueError("DataTap token must not be empty")
        if max_concurrency_per_service < 1:
            raise ValueError("max_concurrency_per_service must be positive")
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be positive")
        if circuit_reset_seconds <= 0 or queue_timeout_seconds <= 0:
            raise ValueError("timeouts must be positive")

        self.gateway_session_id = gateway_session_id or str(uuid4())
        if not self.gateway_session_id.strip() or not credential_version.strip():
            raise ValueError("session and credential identifiers must not be empty")
        self.credential_version = credential_version
        self.failure_threshold = failure_threshold
        self._circuit_reset_seconds = circuit_reset_seconds
        self._queue_timeout_seconds = queue_timeout_seconds
        self._read_timeout_seconds = read_timeout_seconds
        self._clock = clock
        self._session_opener = session_opener
        self._session_factory = session_factory
        self._states = {
            service: _ServiceState(asyncio.Semaphore(max_concurrency_per_service))
            for service in DataTapService
        }
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {secret}"},
            timeout=httpx.Timeout(
                connect=connect_timeout_seconds,
                read=read_timeout_seconds,
                write=write_timeout_seconds,
                pool=pool_timeout_seconds,
            ),
            follow_redirects=False,
            trust_env=False,
            verify=True,
            transport=http_transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def protocol_session_digest(self, service: DataTapService) -> str:
        checked = self._require_service(service)
        scoped_identity = "\x00".join(
            (self.gateway_session_id, checked.value, self.credential_version)
        )
        return hashlib.sha256(scoped_identity.encode("utf-8")).hexdigest()

    async def list_tools(self, service: DataTapService) -> tuple[DiscoveredTool, ...]:
        checked = self._require_service(service)

        async def operation() -> tuple[DiscoveredTool, ...]:
            async with self._session_opener(
                self._endpoint(checked),
                http_client=self._client,
                terminate_on_close=True,
            ) as (read_stream, write_stream, _get_session_id):
                async with self._session_factory(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=timedelta(seconds=self._read_timeout_seconds),
                ) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    return tuple(self._convert_tool(tool) for tool in result.tools)

        return await self._run_isolated(checked, operation)

    async def call_tool(
        self,
        service: DataTapService,
        remote_name: str,
        arguments: Mapping[str, JsonValue],
    ) -> RemoteToolResult:
        checked = self._require_service(service)
        if not remote_name or not isinstance(remote_name, str):
            raise TypeError("remote_name must be a non-empty string")

        async def operation() -> RemoteToolResult:
            try:
                async with self._session_opener(
                    self._endpoint(checked),
                    http_client=self._client,
                    terminate_on_close=True,
                ) as (read_stream, write_stream, _get_session_id):
                    async with self._session_factory(
                        read_stream,
                        write_stream,
                        read_timeout_seconds=timedelta(seconds=self._read_timeout_seconds),
                    ) as session:
                        await session.initialize()
                        try:
                            result = await session.call_tool(remote_name, dict(arguments))
                        except McpError as exc:
                            if exc.error.code == httpx.codes.REQUEST_TIMEOUT:
                                raise PossiblySentTimeout("MCP result was not confirmed") from exc
                            raise McpProtocolError("MCP tool protocol error") from exc
                        except (httpx.ReadTimeout, TimeoutError) as exc:
                            raise PossiblySentTimeout("MCP result was not confirmed") from exc
                        structured_content = getattr(result, "structuredContent", None)
                        error_text = None
                        if getattr(result, "isError", False):
                            error_text = self._error_text(result)
                        return RemoteToolResult(
                            structured_content=structured_content,
                            is_error=bool(getattr(result, "isError", False)),
                            upstream_request_id=self._request_id(result),
                            error_text=error_text,
                        )
            except PossiblySentTimeout:
                raise
            except BaseException as exc:
                if self._contains_exception(exc, httpx.ConnectTimeout):
                    raise McpConnectionTimeout("MCP endpoint connection timed out") from exc
                if self._contains_exception(exc, httpx.ConnectError):
                    raise McpConnectionError("MCP endpoint connection failed") from exc
                if self._contains_exception(exc, (httpx.ReadTimeout, TimeoutError)):
                    raise PossiblySentTimeout("MCP result was not confirmed") from exc
                status_error = self._find_exception(exc, httpx.HTTPStatusError)
                if status_error is not None:
                    status_code = status_error.response.status_code
                    if status_code in {408, 504}:
                        raise McpGatewayTimeout("MCP gateway timed out") from exc
                    if status_code >= 500:
                        raise McpUpstreamHttpError("MCP gateway returned an upstream error") from exc
                if isinstance(exc, McpUpstreamError):
                    raise
                if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    raise
                raise McpProtocolError("MCP protocol operation failed") from exc

        return await self._run_isolated(checked, operation)

    def _require_service(self, service: DataTapService) -> DataTapService:
        if isinstance(service, str) and service in _DISABLED_SERVICES:
            raise ServiceNotAllowedError("DataTap service is disabled")
        if not isinstance(service, DataTapService):
            raise TypeError("service must be a DataTapService")
        return service

    @staticmethod
    def _endpoint(service: DataTapService) -> str:
        return f"{_DATATAP_ORIGIN}/api/gateway/{service.value}/mcp"

    async def _run_isolated(self, service: DataTapService, operation: Callable[[], Any]):
        state = self._states[service]
        try:
            await asyncio.wait_for(state.semaphore.acquire(), timeout=self._queue_timeout_seconds)
        except TimeoutError as exc:
            raise McpQueueTimeout("MCP service concurrency queue timed out") from exc

        try:
            epoch = await self._enter_circuit(state)
            try:
                result = await operation()
            except Exception as exc:
                await self._record_failure(state, epoch)
                if isinstance(exc, PossiblySentTimeout):
                    raise
                if isinstance(exc, McpUpstreamError):
                    raise
                if isinstance(exc, httpx.ConnectTimeout):
                    raise McpConnectionTimeout("MCP endpoint connection timed out") from exc
                if isinstance(exc, httpx.ConnectError):
                    raise McpConnectionError("MCP endpoint connection failed") from exc
                if isinstance(exc, (httpx.ReadTimeout, TimeoutError)):
                    raise PossiblySentTimeout("MCP result was not confirmed") from exc
                if isinstance(exc, (httpx.HTTPError, TimeoutError, OSError)):
                    raise McpUpstreamError("MCP upstream request failed") from exc
                raise McpUpstreamError("MCP protocol operation failed") from exc
            await self._record_success(state, epoch)
            return result
        finally:
            state.semaphore.release()

    async def _enter_circuit(self, state: _ServiceState) -> int:
        async with state.lock:
            if state.opened_at is None:
                return state.epoch
            if self._clock() - state.opened_at < self._circuit_reset_seconds:
                raise McpCircuitOpen("MCP service circuit is open")
            if state.half_open_in_flight:
                raise McpCircuitOpen("MCP service circuit half-open probe is busy")
            state.half_open_in_flight = True
            return state.epoch

    async def _record_failure(self, state: _ServiceState, epoch: int) -> None:
        async with state.lock:
            if epoch != state.epoch:
                return
            state.failures += 1
            if state.half_open_in_flight or state.failures >= self.failure_threshold:
                state.opened_at = self._clock()
                state.epoch += 1
            state.half_open_in_flight = False

    async def _record_success(self, state: _ServiceState, epoch: int) -> None:
        async with state.lock:
            if epoch != state.epoch:
                return
            was_half_open = state.half_open_in_flight
            state.failures = 0
            state.opened_at = None
            state.half_open_in_flight = False
            if was_half_open:
                state.epoch += 1

    @staticmethod
    def _convert_tool(tool: Any) -> DiscoveredTool:
        input_schema = getattr(tool, "inputSchema", None)
        output_schema = getattr(tool, "outputSchema", None)
        if not isinstance(input_schema, dict):
            raise McpUpstreamError("MCP tool input schema is invalid")
        if output_schema is not None and not isinstance(output_schema, dict):
            raise McpUpstreamError("MCP tool output schema is invalid")
        return DiscoveredTool(
            name=tool.name,
            description=getattr(tool, "description", None),
            input_schema=input_schema,
            output_schema=output_schema,
        )

    @staticmethod
    def _error_text(result: Any) -> str | None:
        """拼接 MCP 错误结果的文本内容并截断，供回喂模型自我纠正。"""
        parts: list[str] = []
        for item in getattr(result, "content", None) or []:
            text = getattr(item, "text", None)
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        if not parts:
            return None
        return " ".join(parts)[:500]

    @staticmethod
    def _request_id(result: Any) -> str | None:
        metadata = getattr(result, "meta", None) or getattr(result, "_meta", None)
        if isinstance(metadata, dict):
            value = metadata.get("requestId") or metadata.get("request_id")
            return value if isinstance(value, str) else None
        return None

    @staticmethod
    def _contains_exception(exc: BaseException, expected: type[BaseException] | tuple[type[BaseException], ...]) -> bool:
        if isinstance(exc, expected):
            return True
        if isinstance(exc, BaseExceptionGroup):
            return any(
                DataTapTransport._contains_exception(child, expected)
                for child in exc.exceptions
            )
        return False

    @staticmethod
    def _find_exception(
        exc: BaseException,
        expected: type[BaseException] | tuple[type[BaseException], ...],
    ) -> BaseException | None:
        if isinstance(exc, expected):
            return exc
        if isinstance(exc, BaseExceptionGroup):
            for child in exc.exceptions:
                found = DataTapTransport._find_exception(child, expected)
                if found is not None:
                    return found
        return None
