"""模型决策墙钟与流式故障收口（J1）。

- 每次 create 尝试（含整个流消费）受 ``decision_timeout_seconds`` 墙钟约束：
  超时取消本次尝试、按可重试 MODEL_TIMEOUT 在重试预算内重试，最终失败才抛出；
- 流结束无 finish_reason（ModelStreamInterrupted）在重试预算内可重试；
- MODEL_TIMEOUT 分类为可重试（受墙钟与 attempts 双重上界约束）。

三条路径（成功 / 重试后成功 / 明确失败）都有确定时间上界。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from app.model.contracts import (
    ChatMessage,
    ModelAdapterError,
    ModelStreamInterrupted,
    StructuredModelRequest,
)
from app.model.prompt_logs import PromptLogEntry
from app.model.tencent_plan import TencentPlanAdapter


class _Out(BaseModel):
    value: int


def _request(**overrides: Any) -> StructuredModelRequest[_Out]:
    values: dict[str, Any] = {
        "purpose": "agent_loop",
        "template_name": "agent_loop_v1",
        "messages": (
            ChatMessage(role="system", content="sys"),
            ChatMessage(role="user", content="user"),
        ),
        "output_model": _Out,
    }
    values.update(overrides)
    return StructuredModelRequest(**values)


class _CaptureWriter:
    def __init__(self) -> None:
        self.entries: list[PromptLogEntry] = []

    async def __call__(self, entry: PromptLogEntry) -> None:
        self.entries.append(entry)


class CaptureThinkingSink:
    def __init__(self) -> None:
        self.deltas: list[tuple[int, str]] = []
        self.terminals: list[tuple[str, int]] = []

    async def started(self, *, attempt: int) -> None:
        return None

    async def delta(self, text: str, *, attempt: int) -> None:
        self.deltas.append((attempt, text))

    async def completed(self, *, attempt: int, duration_ms: int) -> None:
        self.terminals.append(("completed", attempt))

    async def failed(self, *, attempt: int, error_code: str) -> None:
        self.terminals.append(("failed", attempt))


async def _no_backoff(_: float) -> None:
    return None


class FakeCompletions:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _chunk(content: str | None, finish_reason: str | None = None) -> Any:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=content, reasoning_content=None),
                finish_reason=finish_reason,
            )
        ],
        usage=None,
        _request_id="req-timeout",
    )


def good_stream(content: str = '{"value":4}') -> Any:
    chunks = [_chunk(content), _chunk(None, finish_reason="stop")]

    async def stream() -> Any:
        for chunk in chunks:
            yield chunk

    return stream()


def trickle_stream(hang_seconds: float = 60.0) -> Any:
    """持续有零碎数据但永不结束的流：httpx 读超时永不触发，只有决策墙钟能收口。"""

    async def stream() -> Any:
        yield _chunk("<think>分析")
        await asyncio.sleep(hang_seconds)
        yield _chunk("更多")

    return stream()


def unfinished_stream() -> Any:
    """正常返回数据但流结束无 finish_reason（连接中断类）。"""

    async def stream() -> Any:
        yield _chunk('{"value":4')

    return stream()


def json_response(content: str) -> Any:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason="stop")],
        usage=None,
        _request_id="req-non-stream",
    )


class HangingCompletions:
    """非流式 create 永不返回（响应持续 trickle 时 httpx 读超时同样失效）。"""

    def __init__(self, hang_seconds: float = 60.0) -> None:
        self.calls: list[dict[str, Any]] = []
        self._hang_seconds = hang_seconds

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        await asyncio.sleep(self._hang_seconds)
        raise AssertionError("unreachable: 决策墙钟必须先取消本次尝试")


def _adapter(client: Any, **overrides: Any) -> TencentPlanAdapter:
    values: dict[str, Any] = {
        "client": client,
        "log_writer": _CaptureWriter(),
        "stream_support_cache": {},
        "sleep": _no_backoff,
        "decision_timeout_seconds": 0.05,
    }
    values.update(overrides)
    return TencentPlanAdapter(**values)


@pytest.mark.asyncio
async def test_stream_decision_wall_clock_retries_and_succeeds() -> None:
    """trickle 流触发决策级墙钟 → 取消本次尝试 → 预算内重试 → 成功。"""
    sink = CaptureThinkingSink()
    client = FakeCompletions([trickle_stream(), good_stream()])
    adapter = _adapter(client)

    started = time.monotonic()
    result = await adapter.complete_json(_request(thinking_sink=sink))
    elapsed = time.monotonic() - started

    assert result.value.value == 4
    assert len(client.calls) == 2
    # 总耗时有上界：墙钟 0.05s 级，绝不是 trickle 流的 60s。
    assert elapsed < 5
    assert sink.terminals == [("completed", 1)]


@pytest.mark.asyncio
async def test_stream_decision_wall_clock_exhausts_budget_and_fails() -> None:
    """每次尝试都触发墙钟：预算耗尽后明确失败 MODEL_TIMEOUT（可重试分类）。"""
    sink = CaptureThinkingSink()
    client = FakeCompletions([trickle_stream(), trickle_stream(), trickle_stream()])
    adapter = _adapter(client, max_attempts=3)

    started = time.monotonic()
    with pytest.raises(ModelAdapterError, match="MODEL_TIMEOUT") as exc_info:
        await adapter.complete_json(_request(thinking_sink=sink))
    elapsed = time.monotonic() - started

    assert exc_info.value.retryable is True
    assert len(client.calls) == 3
    assert elapsed < 5
    assert sink.terminals == [("failed", 1)]


@pytest.mark.asyncio
async def test_non_stream_decision_wall_clock_exhausts_budget_and_fails() -> None:
    """无 sink 的非流式决策同样受墙钟约束：挂死尝试被取消并按预算重试。"""
    client = HangingCompletions()
    adapter = _adapter(client, max_attempts=2)

    started = time.monotonic()
    with pytest.raises(ModelAdapterError, match="MODEL_TIMEOUT") as exc_info:
        await adapter.complete_json(_request())
    elapsed = time.monotonic() - started

    assert exc_info.value.retryable is True
    assert len(client.calls) == 2
    assert all(call["stream"] is False for call in client.calls)
    assert elapsed < 5


@pytest.mark.asyncio
async def test_stream_end_without_finish_reason_retries_and_succeeds() -> None:
    """流结束无 finish_reason（ModelStreamInterrupted）在预算内可重试。"""
    sink = CaptureThinkingSink()
    client = FakeCompletions([unfinished_stream(), good_stream()])
    adapter = _adapter(client)

    result = await adapter.complete_json(_request(thinking_sink=sink))

    assert result.value.value == 4
    assert len(client.calls) == 2
    assert sink.terminals == [("completed", 1)]


@pytest.mark.asyncio
async def test_stream_end_without_finish_reason_exhausts_budget_and_fails() -> None:
    """连续无 finish_reason：预算耗尽后明确失败 MODEL_STREAM_INTERRUPTED。"""
    sink = CaptureThinkingSink()
    client = FakeCompletions([unfinished_stream() for _ in range(3)])
    adapter = _adapter(client, max_attempts=3)

    with pytest.raises(ModelStreamInterrupted, match="MODEL_STREAM_INTERRUPTED"):
        await adapter.complete_json(_request(thinking_sink=sink))

    assert len(client.calls) == 3
    assert sink.terminals == [("failed", 1)]


def test_model_timeout_mapped_as_retryable() -> None:
    """MODEL_TIMEOUT 改可重试：受决策墙钟与 attempts 预算双重上界约束。"""
    adapter = _adapter(FakeCompletions([]))

    mapped = adapter._map_error(asyncio.TimeoutError())

    assert mapped.code == "MODEL_TIMEOUT"
    assert mapped.retryable is True
