from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Literal
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
    McpNotSentError,
    McpProtocolError,
    McpQueueTimeout,
    McpUpstreamHttpError,
    McpUpstreamError,
    PossiblySentTimeout,
    RemoteToolResult,
    ServiceNotAllowedError,
    contains_transport_artifact_marker,
    is_non_empty_json,
)

logger = logging.getLogger(__name__)


_DEFAULT_DATATAP_ORIGIN = "https://datatap.deepminer.com.cn"


def _datatap_origin() -> str:
    """生产默认指向真实 DataTap；测试/离线拓扑经 Settings 覆盖到 loopback。"""
    from app.core.config import get_settings

    return get_settings().datatap_mcp_origin.rstrip("/")
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
        read_timeout_seconds: float = 60.0,
        write_timeout_seconds: float = 10.0,
        pool_timeout_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
        # "service"：legacy 服务级熔断（默认，行为不变）。
        # "none"：Agent 桥固定使用，服务级熔断不再维护 open 状态，改由
        #   agent_runtime.circuit_breaker 的 service+tool+args-hash 细粒度熔断
        #   单独负责；队列并发限制与超时仍然生效。禁止两层熔断叠加。
        circuit_scope: Literal["service", "none"] = "service",
        # "never"（默认）：任何失败都不自动重发——504、5xx、协议中断与
        #   PossiblySentTimeout 都属"可能已发送"（result_unknown），设计
        #   §5.3/§11.1 禁止自动重放；明确的连接前失败交由模型决定是否重新尝试。
        # "transient_once"：legacy 策略，瞬时上游错误自动重试一次。
        retry_policy: Literal["transient_once", "never"] = "never",
        # 外发阶段墙钟上限（秒，不含 per-service 队列等待）：None（默认）不启用，
        #   legacy 行为不变；Agent 传输经 AGENT_MCP_CALL_TIMEOUT_SECONDS 注入。
        #   DataTap 统计查询可能持续 trickle 返回数据，httpx read_timeout 是
        #   "无活动"超时会被不断重置（UAT Incident #8：一次慢查询挂死整个 Run）。
        #   超时按 PossiblySentTimeout（可能已发送）收口，由上层分类为
        #   result_unknown（保留预留、进恢复核对），Run 继续后续工具。
        call_timeout_seconds: float | None = None,
        # 取消宽限：墙钟超时后取消底层任务并等待其退出的上限；仍不死则隔离
        #   悬挂任务（保留引用防 GC、完成时吞噬异常），运行时侧按时收口。
        cancel_grace_seconds: float = 5.0,
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
        if circuit_scope not in ("service", "none"):
            raise ValueError("circuit_scope must be 'service' or 'none'")
        if retry_policy not in ("transient_once", "never"):
            raise ValueError("retry_policy must be 'transient_once' or 'never'")
        if call_timeout_seconds is not None and call_timeout_seconds <= 0:
            raise ValueError("call_timeout_seconds must be positive")
        if cancel_grace_seconds <= 0:
            raise ValueError("cancel_grace_seconds must be positive")
        self.circuit_scope = circuit_scope
        self.retry_policy = retry_policy

        self.gateway_session_id = gateway_session_id or str(uuid4())
        if not self.gateway_session_id.strip() or not credential_version.strip():
            raise ValueError("session and credential identifiers must not be empty")
        self.credential_version = credential_version
        self.failure_threshold = failure_threshold
        self._circuit_reset_seconds = circuit_reset_seconds
        self._queue_timeout_seconds = queue_timeout_seconds
        self._read_timeout_seconds = read_timeout_seconds
        self._call_timeout_seconds = call_timeout_seconds
        self._cancel_grace_seconds = cancel_grace_seconds
        # 墙钟超时后取消仍不死的悬挂任务：保留引用防 GC，完成时吞噬异常。
        self._abandoned: set[asyncio.Task[Any]] = set()
        self._clock = clock
        self._session_opener = session_opener
        self._session_factory = session_factory
        self._states = {
            service: _ServiceState(asyncio.Semaphore(max_concurrency_per_service))
            for service in DataTapService
        }
        # 已确认结果按 upstream_request_id 的受限本地缓存，供 reconcile_tool_call
        # 只读核对（见下）。
        self._recent_results: dict[str, RemoteToolResult] = {}
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
                        try:
                            await session.initialize()
                        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                            raise
                        except Exception as exc:
                            # Session initialization happens before the MCP
                            # request is dispatched.  Preserve that fact for
                            # billing: it is a confirmed non-send, not an
                            # unknown result.
                            raise McpNotSentError("MCP session initialization failed") from exc
                        try:
                            result = await session.call_tool(remote_name, dict(arguments))
                        except McpError as exc:
                            if exc.error.code == httpx.codes.REQUEST_TIMEOUT:
                                raise PossiblySentTimeout("MCP result was not confirmed") from exc
                            raise McpProtocolError("MCP tool protocol error") from exc
                        except (httpx.ReadTimeout, TimeoutError) as exc:
                            raise PossiblySentTimeout("MCP result was not confirmed") from exc
                        structured_content, result_status, unavailable_reason = (
                            self._classify_result(result)
                        )
                        error_text = None
                        if getattr(result, "isError", False):
                            error_text = self._error_text(result)
                        return RemoteToolResult(
                            structured_content=structured_content,
                            is_error=bool(getattr(result, "isError", False)),
                            upstream_request_id=self._request_id(result),
                            error_text=error_text,
                            result_status=result_status,
                            unavailable_reason=unavailable_reason,
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

        result = await self._run_isolated_with_retry(checked, operation)
        if result.upstream_request_id:
            self._record_recent(result)
        return result

    async def reconcile_tool_call(self, upstream_request_id: str) -> RemoteToolResult | None:
        """READ ONLY：按 upstream_request_id 回查本地已确认结果，绝不重放原调用。

        调用结果在 :meth:`call_tool` 返回时按 ``upstream_request_id`` 记录在
        受控大小的本地缓存；恢复流程据此确认 result_unknown，不重新外发
        原调用（§11.1「禁止自动重放」）。
        """
        if not upstream_request_id:
            return None
        return self._recent_results.get(upstream_request_id)

    _MAX_RECENT_RESULTS = 1_000

    def _record_recent(self, result: RemoteToolResult) -> None:
        request_id = result.upstream_request_id
        if request_id is None:
            return
        if len(self._recent_results) >= self._MAX_RECENT_RESULTS:
            oldest = next(iter(self._recent_results))
            self._recent_results.pop(oldest, None)
        self._recent_results[request_id] = result

    async def _run_isolated_with_retry(
        self, service: DataTapService, operation: Callable[[], Any]
    ):
        """按 ``retry_policy`` 决定是否自动重发。

        - ``"never"``（默认）：绝不重发。504、5xx、协议中断与
          PossiblySentTimeout 都属"可能已发送"（result_unknown），自动重放
          可能重复执行上游查询（设计 §5.3/§11.1 禁止）；
        - ``"transient_once"``（legacy）：瞬时上游错误（5xx/网关超时/连接
          失败）自动重试一次。PossiblySentTimeout 仍不在重试之列；熔断器/
          队列类错误立即抛出（立即重试同样会被拒绝）。
        """
        if self.retry_policy == "never":
            return await self._run_isolated(service, operation)
        try:
            return await self._run_isolated(service, operation)
        except (
            McpUpstreamHttpError,
            McpGatewayTimeout,
            McpConnectionTimeout,
            McpConnectionError,
        ):
            await asyncio.sleep(0.3)
            return await self._run_isolated(service, operation)

    def _require_service(self, service: DataTapService) -> DataTapService:
        if isinstance(service, str) and service in _DISABLED_SERVICES:
            raise ServiceNotAllowedError("DataTap service is disabled")
        if not isinstance(service, DataTapService):
            raise TypeError("service must be a DataTapService")
        return service

    @staticmethod
    def _endpoint(service: DataTapService) -> str:
        return f"{_datatap_origin()}/api/gateway/{service.value}/mcp"

    async def _run_isolated(self, service: DataTapService, operation: Callable[[], Any]):
        state = self._states[service]
        try:
            await asyncio.wait_for(state.semaphore.acquire(), timeout=self._queue_timeout_seconds)
        except TimeoutError as exc:
            raise McpQueueTimeout("MCP service concurrency queue timed out") from exc

        try:
            epoch = await self._enter_circuit(state)
            try:
                result = await self._dispatch_with_wall_clock(operation)
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

    async def _dispatch_with_wall_clock(self, operation: Callable[[], Any]):
        """外发阶段墙钟上限（``call_timeout_seconds``，仅 Agent 传输启用）。

        超时即按 :class:`PossiblySentTimeout`（可能已发送）收口——取消底层
        任务并在 ``cancel_grace_seconds`` 宽限内等待其真正退出；仍不死
        （某层吞掉取消）则隔离悬挂任务，运行时侧按时收口，绝不挂死调用方。
        外层被取消（引擎停机/租约让渡）时同样取消并隔离内层任务，避免
        悬挂请求在后台静默完成后无人收口。

        不使用 ``asyncio.wait_for``：它在超时后会无限期等待被取消任务退出，
        底层不可取消时依旧挂死（UAT Incident #8 的教训）。
        """
        if self._call_timeout_seconds is None:
            return await operation()
        task = asyncio.ensure_future(operation())
        try:
            done, _pending = await asyncio.wait({task}, timeout=self._call_timeout_seconds)
            if done:
                return task.result()  # 异常原样上抛，保持既有故障分类
            task.cancel()
            done, _pending = await asyncio.wait({task}, timeout=self._cancel_grace_seconds)
            if not done:
                logger.warning(
                    "MCP dispatch survived cancellation after wall-clock timeout; "
                    "abandoning hung task (outcome unconfirmed)"
                )
            raise PossiblySentTimeout("MCP call exceeded wall-clock timeout")
        finally:
            if not task.done():
                task.cancel()
                self._track_abandoned(task)

    def _track_abandoned(self, task: asyncio.Task[Any]) -> None:
        """隔离悬挂任务：保留引用防 GC，完成时吞噬异常并记录，绝不影响后续调用。"""
        if task in self._abandoned:
            return
        self._abandoned.add(task)

        def _consume(finished: asyncio.Task[Any]) -> None:
            self._abandoned.discard(finished)
            if finished.cancelled():
                return
            exc = finished.exception()
            if exc is not None:
                logger.warning("abandoned MCP dispatch finished with error: %r", exc)
            else:
                logger.warning(
                    "abandoned MCP dispatch finished after the caller was cut off; "
                    "result discarded (reservation stays with recovery reconcile)"
                )

        task.add_done_callback(_consume)

    async def _enter_circuit(self, state: _ServiceState) -> int:
        if self.circuit_scope == "none":
            # 服务级熔断不参与 Agent 路径；立即放行。
            return state.epoch
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
        if self.circuit_scope == "none":
            return
        async with state.lock:
            if epoch != state.epoch:
                return
            state.failures += 1
            if state.half_open_in_flight or state.failures >= self.failure_threshold:
                state.opened_at = self._clock()
                state.epoch += 1
            state.half_open_in_flight = False

    async def _record_success(self, state: _ServiceState, epoch: int) -> None:
        if self.circuit_scope == "none":
            return
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
    def _structured_content(result: Any) -> Any:
        """返回严格规范化后的 payload；不可信文本一律返回 ``None``。

        需要区分 ``None`` 的 genuinely empty 与 unavailable，实际传输路径使用
        :meth:`_classify_result` 取得状态；此兼容 helper 只保留旧测试/调用方的
        payload 视图，绝不再把普通文本包装成可写 Evidence 的对象。
        """
        payload, _status, _reason = DataTapTransport._classify_result(result)
        return payload

    @staticmethod
    def _classify_result(result: Any) -> tuple[Any, Literal["available", "empty", "unavailable"], str | None]:
        """把真实 MCP CallToolResult 归一化为三态结果。

        只有 native ``structuredContent``，或唯一且整体可解析的 JSON text block
        才能进入 available。resource/image/audio、多个 text block、普通文本、
        临时路径和解析失败都进入 unavailable；没有任何 content 才是 empty。
        """
        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            if is_non_empty_json(structured):
                if contains_transport_artifact_marker(structured):
                    return None, "unavailable", "unsupported_content"
                return structured, "available", None
            # Native structuredContent is authoritative when present. An empty
            # native value must not fall through to an unrelated text/resource
            # block and become a different payload.
            try:
                json.dumps(structured, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError, RecursionError):
                return None, "unavailable", "unsupported_content"
            return None, "empty", None
        if getattr(result, "isError", False):
            return None, "empty", None

        blocks = getattr(result, "content", None)
        if blocks is None or (isinstance(blocks, list) and len(blocks) == 0):
            return None, "empty", None
        if not isinstance(blocks, list) or len(blocks) != 1:
            return None, "unavailable", "unsupported_content"
        block = blocks[0]
        text = getattr(block, "text", None)
        if getattr(block, "type", None) != "text" or not isinstance(text, str):
            return None, "unavailable", "unsupported_content"
        if not text.strip():
            return None, "empty", None
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None, "unavailable", "invalid_json_text"
        if not is_non_empty_json(parsed):
            return None, "empty", None
        if contains_transport_artifact_marker(parsed):
            return None, "unavailable", "unsupported_content"
        return parsed, "available", None

    @staticmethod
    def _error_text(result: Any) -> str | None:
        """拼接 MCP 错误结果的文本内容并截断，供回喂模型自我纠正。"""
        return DataTapTransport._content_text(result, limit=500)

    @staticmethod
    def _content_text(result: Any, *, limit: int) -> str | None:
        parts: list[str] = []
        for item in getattr(result, "content", None) or []:
            text = getattr(item, "text", None)
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        if not parts:
            return None
        return " ".join(parts)[:limit]

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
