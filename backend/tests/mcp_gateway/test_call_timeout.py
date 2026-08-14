"""DataTap 传输墙钟超时（cutover 阻断项 1 / UAT Incident #8）。

真实 DataTap 统计查询可能长时间持续 trickle 返回数据：httpx ``read_timeout``
是"无活动"超时，数据持续到达时被不断重置永不触发，``ClientSession`` 的
读取超时同样被流式进度吞掉——一次慢查询即可挂死整个 Run。

Agent 传输（``get_agent_mcp_transport``）配置 ``call_timeout_seconds`` 后，
**外发阶段**（拿到 per-service 队列许可之后，队列等待不计入预算）超过墙钟
上限即：取消底层任务 → 宽限内等待其真正退出 → 仍不死则隔离悬挂任务
（保留引用防 GC、完成时吞噬异常），并按 ``PossiblySentTimeout``（可能已发送）
收口——由上层 ``AgentMcpTool`` 分类为 result_unknown（保留预留、进恢复核对），
Run 继续后续工具。legacy 传输缺省不启用，行为不变。
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from app.mcp_gateway.contracts import DataTapService
from app.mcp_gateway.datatap import DataTapTransport
from app.mcp_gateway.transport import (
    McpConnectionTimeout,
    PossiblySentTimeout,
)

CALL_TIMEOUT = 0.3
CANCEL_GRACE = 0.3


class _BaseSession:
    def __init__(self, read_stream, write_stream, **_kwargs) -> None:
        self.service = read_stream

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def initialize(self) -> None:
        return None


class HangingSession(_BaseSession):
    """持续 trickle 的统计查询：永不返回结果（read timeout 永不触发）。"""

    call_count = 0

    async def call_tool(self, _name, _arguments):
        type(self).call_count += 1
        while True:  # 数据持续到达的等待循环：每次 sleep 都被取消打断
            await asyncio.sleep(0.05)


class StubbornSession(_BaseSession):
    """吞掉取消的顽固层：第一次 CancelledError 被吃掉后继续挂起。

    用于验证"某层确实不可取消"时的兜底：运行时侧仍须按时收口，
    悬挂任务被隔离而不是卡住调用方。
    """

    released: asyncio.Event | None = None
    swallowed_cancel = 0

    async def call_tool(self, _name, _arguments):
        try:
            await type(self).released.wait()
        except asyncio.CancelledError:
            type(self).swallowed_cancel += 1
            await type(self).released.wait()  # 顽固：吞掉取消继续挂
        return SimpleNamespace(structuredContent={"result": "{}"}, isError=False, content=[])


class SlowSession(_BaseSession):
    """慢于队列等待、但快于墙钟上限的正常调用。"""

    sleep_seconds = 0.0

    async def call_tool(self, _name, _arguments):
        await asyncio.sleep(type(self).sleep_seconds)
        return SimpleNamespace(
            structuredContent={"result": "ok"}, isError=False, content=[],
            meta={"requestId": "req-slow"},
        )


class ConnectTimeoutSession(_BaseSession):
    async def call_tool(self, _name, _arguments):
        import httpx

        raise httpx.ConnectTimeout("connect timeout")


def _opener():
    @asynccontextmanager
    async def opener(url: str, **_kwargs):
        service = next(item for item in DataTapService if item.value in url)
        yield service, object(), lambda: "session-1"

    return opener


def _transport(session_factory, **kwargs) -> DataTapTransport:
    return DataTapTransport(
        token=SecretStr("unit-test-token"),
        session_opener=_opener(),
        session_factory=session_factory,
        call_timeout_seconds=CALL_TIMEOUT,
        cancel_grace_seconds=CANCEL_GRACE,
        **kwargs,
    )


async def test_hanging_trickling_call_is_cut_off_as_possibly_sent_timeout() -> None:
    """持续 trickle 的挂起调用在墙钟上限内以 PossiblySentTimeout 收口。"""
    HangingSession.call_count = 0
    transport = _transport(HangingSession)

    started = time.monotonic()
    with pytest.raises(PossiblySentTimeout):
        await transport.call_tool(DataTapService.BILIBILI, "search", {"keyword": "美妆"})
    elapsed = time.monotonic() - started

    assert elapsed < CALL_TIMEOUT + CANCEL_GRACE + 1.0
    assert HangingSession.call_count == 1


async def test_uncancellable_layer_is_still_cut_off_on_time_and_isolated() -> None:
    """底层吞掉取消时：运行时侧按时收口，悬挂任务被隔离（不泄漏、不告警异常）。"""
    StubbornSession.released = asyncio.Event()
    StubbornSession.swallowed_cancel = 0
    transport = _transport(StubbornSession)

    started = time.monotonic()
    with pytest.raises(PossiblySentTimeout):
        await transport.call_tool(DataTapService.BILIBILI, "search", {"keyword": "美妆"})
    elapsed = time.monotonic() - started

    # 取消被吞掉 → 等满宽限后放弃，但总耗时仍受控（超时 + 宽限 + 少量调度余量）
    assert elapsed < CALL_TIMEOUT + CANCEL_GRACE + 1.0
    assert StubbornSession.swallowed_cancel >= 1
    assert len(transport._abandoned) == 1

    # 隔离的悬挂任务稍后结束：异常/结果被吞噬，不影响事件循环
    StubbornSession.released.set()
    abandoned = list(transport._abandoned)
    await asyncio.gather(*abandoned, return_exceptions=True)
    await asyncio.sleep(0)
    assert not transport._abandoned


async def test_fast_call_is_unaffected_by_wall_clock() -> None:
    """正常快调用不受墙钟影响：结果、request id 记录与既有行为一致。"""
    SlowSession.sleep_seconds = 0.01
    transport = _transport(SlowSession)

    result = await transport.call_tool(DataTapService.BILIBILI, "search", {"keyword": "美妆"})

    assert result.is_error is False
    assert result.structured_content == {"result": "ok"}
    assert result.upstream_request_id == "req-slow"
    # 已确认结果仍按 upstream_request_id 缓存，供恢复核对
    assert await transport.reconcile_tool_call("req-slow") is result


async def test_queue_wait_does_not_consume_wall_clock_budget() -> None:
    """墙钟从外发开始计：per-service 队列等待不消耗预算。

    第一个调用挂起占住许可直到超时；第二个调用排队约 CALL_TIMEOUT 后才外发，
    自身执行 0.9 * CALL_TIMEOUT < 上限——若队列等待计入预算它必然超时。
    """
    HangingSession.call_count = 0
    SlowSession.sleep_seconds = CALL_TIMEOUT * 0.9

    class FirstHangThenSlow(_BaseSession):
        async def call_tool(self, _name, _arguments):
            if HangingSession.call_count == 0:
                HangingSession.call_count += 1
                await asyncio.Event().wait()
                raise AssertionError("unreachable")
            await asyncio.sleep(SlowSession.sleep_seconds)
            return SimpleNamespace(
                structuredContent={"result": "second"}, isError=False, content=[],
                meta={"requestId": "req-second"},
            )

    transport = _transport(FirstHangThenSlow, max_concurrency_per_service=1)

    started = time.monotonic()
    with pytest.raises(PossiblySentTimeout):
        await transport.call_tool(DataTapService.BILIBILI, "search", {"round": 1})

    # 队列许可已随超时释放：同服务下一个调用可以继续（Run 继续后续工具）
    result = await transport.call_tool(DataTapService.BILIBILI, "search", {"round": 2})
    total = time.monotonic() - started

    assert result.structured_content == {"result": "second"}
    assert total > CALL_TIMEOUT + SlowSession.sleep_seconds


async def test_non_timeout_classification_is_preserved_under_wall_clock() -> None:
    """配置墙钟后既有故障分类不变：连接前超时仍是 definitely-not-sent 类。"""
    transport = _transport(ConnectTimeoutSession)

    with pytest.raises(McpConnectionTimeout):
        await transport.call_tool(DataTapService.BILIBILI, "search", {"keyword": "美妆"})


async def test_wall_clock_disabled_by_default() -> None:
    """缺省（legacy）不启用墙钟：挂起调用只能靠外部取消打断。"""
    transport = DataTapTransport(
        token=SecretStr("unit-test-token"),
        session_opener=_opener(),
        session_factory=HangingSession,
    )
    assert transport._call_timeout_seconds is None

    task = asyncio.create_task(
        transport.call_tool(DataTapService.BILIBILI, "search", {"keyword": "美妆"})
    )
    await asyncio.sleep(0.1)
    assert not task.done()  # 无墙钟：仍在挂起
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_invalid_timeout_configuration_is_rejected() -> None:
    with pytest.raises(ValueError):
        DataTapTransport(token=SecretStr("unit-test-token"), call_timeout_seconds=0)
    with pytest.raises(ValueError):
        DataTapTransport(
            token=SecretStr("unit-test-token"),
            call_timeout_seconds=1.0,
            cancel_grace_seconds=0,
        )
