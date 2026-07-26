"""结构化输出的思考流：JSON 验证与思考内容必须彼此分离。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from app.model.contracts import (
    ChatMessage,
    ModelAdapterError,
    ModelPlanInvalidError,
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
        self.started_attempts: list[int] = []
        self.deltas: list[tuple[int, str]] = []
        self.terminals: list[tuple[str, int]] = []

    @property
    def terminal(self) -> tuple[str, int] | None:
        return self.terminals[-1] if self.terminals else None

    async def started(self, *, attempt: int) -> None:
        self.started_attempts.append(attempt)

    async def delta(self, text: str, *, attempt: int) -> None:
        self.deltas.append((attempt, text))

    async def completed(self, *, attempt: int, duration_ms: int) -> None:
        self.terminals.append(("completed", attempt))

    async def failed(self, *, attempt: int, error_code: str) -> None:
        self.terminals.append(("failed", attempt))


class FailingThinkingSink(CaptureThinkingSink):
    async def started(self, *, attempt: int) -> None:
        raise RuntimeError("sink unavailable")

    async def delta(self, text: str, *, attempt: int) -> None:
        raise RuntimeError("sink unavailable")

    async def completed(self, *, attempt: int, duration_ms: int) -> None:
        raise RuntimeError("sink unavailable")

    async def failed(self, *, attempt: int, error_code: str) -> None:
        raise RuntimeError("sink unavailable")


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


class _StreamUnsupportedError(Exception):
    status_code = 400
    body = {"error": {"message": "stream is not supported", "param": "stream"}}


class _ModelUnsupportedError(Exception):
    status_code = 400
    body = {"error": {"message": "upstream model is not supported"}}


class _ResponseFormatUnsupportedWithValidStreamError(Exception):
    status_code = 400
    body = {
        "error": {
            "message": "model does not support response_format; stream parameter is valid",
            "param": "response_format",
        }
    }


def stream_chunks(
    *,
    content_chunks: list[str | None],
    reasoning_chunks: list[str | None],
    finished: bool = True,
) -> Any:
    chunks = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content=content, reasoning_content=reasoning),
                    finish_reason=None,
                )
            ],
            usage=None,
            _request_id="req-stream",
        )
        for content, reasoning in zip(content_chunks, reasoning_chunks, strict=True)
    ]
    if finished:
        chunks.append(
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content=None, reasoning_content=None),
                        finish_reason="stop",
                    )
                ],
                usage=SimpleNamespace(
                    prompt_tokens=3,
                    completion_tokens=2,
                    total_tokens=5,
                    prompt_tokens_details=None,
                    completion_tokens_details=None,
                ),
                _request_id="req-stream",
            )
        )

    async def stream() -> Any:
        for chunk in chunks:
            yield chunk

    return stream()


def json_response(content: str) -> Any:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason="stop")],
        usage=None,
        _request_id="req-fallback",
    )


@pytest.mark.asyncio
async def test_complete_json_streams_thinking_and_validates_only_json() -> None:
    sink = CaptureThinkingSink()
    client = FakeCompletions(
        [
            stream_chunks(
                content_chunks=["<th", "ink>分析", "品牌</think>", '{"value":4}'],
                reasoning_chunks=[None, None, None, None],
            )
        ]
    )
    adapter = TencentPlanAdapter(client=client, log_writer=_CaptureWriter(), stream_support_cache={})

    result = await adapter.complete_json(_request(thinking_sink=sink))

    assert result.value.value == 4
    assert sink.deltas == [(1, "分析"), (1, "品牌")]
    assert sink.terminal == ("completed", 1)
    assert client.calls[0]["stream"] is True


@pytest.mark.asyncio
async def test_complete_json_publishes_reasoning_content_delta() -> None:
    sink = CaptureThinkingSink()
    client = FakeCompletions(
        [
            stream_chunks(
                content_chunks=[None, '{"value":4}'],
                reasoning_chunks=["先检查品牌", None],
            )
        ]
    )
    adapter = TencentPlanAdapter(client=client, log_writer=_CaptureWriter(), stream_support_cache={})

    result = await adapter.complete_json(_request(thinking_sink=sink))

    assert result.value.value == 4
    assert sink.deltas == [(1, "先检查品牌")]


@pytest.mark.asyncio
async def test_complete_json_stream_repair_uses_new_sink_attempt() -> None:
    sink = CaptureThinkingSink()
    client = FakeCompletions(
        [
            stream_chunks(content_chunks=["{}"], reasoning_chunks=[None]),
            stream_chunks(content_chunks=['{"value":4}'], reasoning_chunks=[None]),
        ]
    )
    adapter = TencentPlanAdapter(client=client, log_writer=_CaptureWriter(), stream_support_cache={})

    result = await adapter.complete_json(_request(thinking_sink=sink))

    assert result.value.value == 4
    assert sink.started_attempts == [1, 2]
    assert sink.terminals == [("failed", 1), ("completed", 2)]


@pytest.mark.asyncio
async def test_complete_json_ignores_thinking_sink_exceptions() -> None:
    client = FakeCompletions(
        [stream_chunks(content_chunks=["<think>分析</think>{\"value\":4}"], reasoning_chunks=[None])]
    )
    adapter = TencentPlanAdapter(client=client, log_writer=_CaptureWriter(), stream_support_cache={})

    result = await adapter.complete_json(_request(thinking_sink=FailingThinkingSink()))

    assert result.value.value == 4


@pytest.mark.asyncio
async def test_complete_json_falls_back_when_stream_is_not_supported() -> None:
    sink = CaptureThinkingSink()
    client = FakeCompletions(
        [_StreamUnsupportedError(), json_response('<think>分析</think>{"value":4}')]
    )
    adapter = TencentPlanAdapter(client=client, log_writer=_CaptureWriter(), stream_support_cache={})

    result = await adapter.complete_json(_request(thinking_sink=sink))

    assert result.value.value == 4
    assert [call["stream"] for call in client.calls] == [True, False]
    assert sink.deltas == [(1, "分析")]
    assert sink.terminal == ("completed", 1)


@pytest.mark.asyncio
async def test_complete_json_does_not_downgrade_for_unsupported_upstream_model() -> None:
    sink = CaptureThinkingSink()
    client = FakeCompletions([_ModelUnsupportedError()])
    adapter = TencentPlanAdapter(client=client, log_writer=_CaptureWriter(), stream_support_cache={})

    with pytest.raises(ModelAdapterError, match="MODEL_UPSTREAM_ERROR"):
        await adapter.complete_json(_request(thinking_sink=sink))

    assert [call["stream"] for call in client.calls] == [True]
    assert sink.terminal == ("failed", 1)


@pytest.mark.asyncio
async def test_complete_json_does_not_downgrade_when_response_format_is_unsupported() -> None:
    sink = CaptureThinkingSink()
    client = FakeCompletions([_ResponseFormatUnsupportedWithValidStreamError()])
    adapter = TencentPlanAdapter(client=client, log_writer=_CaptureWriter(), stream_support_cache={})

    with pytest.raises(ModelAdapterError, match="MODEL_UPSTREAM_ERROR"):
        await adapter.complete_json(_request(thinking_sink=sink))

    assert [call["stream"] for call in client.calls] == [True]
    assert sink.terminal == ("failed", 1)


@pytest.mark.asyncio
async def test_complete_json_fallback_publishes_think_before_repairing_invalid_json() -> None:
    sink = CaptureThinkingSink()
    client = FakeCompletions(
        [
            _StreamUnsupportedError(),
            json_response("<think>第一次分析</think>not-json"),
            json_response('{"value":4}'),
        ]
    )
    adapter = TencentPlanAdapter(client=client, log_writer=_CaptureWriter(), stream_support_cache={})

    result = await adapter.complete_json(_request(thinking_sink=sink))

    assert result.value.value == 4
    assert [call["stream"] for call in client.calls] == [True, False, False]
    assert sink.deltas == [(1, "第一次分析")]
    assert sink.terminals == [("failed", 1), ("completed", 2)]


@pytest.mark.asyncio
async def test_complete_json_stream_interrupt_after_visible_output_does_not_replay() -> None:
    sink = CaptureThinkingSink()
    client = FakeCompletions(
        [stream_chunks(content_chunks=["<think>分析"], reasoning_chunks=[None], finished=False)]
    )
    adapter = TencentPlanAdapter(client=client, log_writer=_CaptureWriter(), stream_support_cache={})

    with pytest.raises(Exception, match="MODEL_STREAM_INTERRUPTED"):
        await adapter.complete_json(_request(thinking_sink=sink))

    assert len(client.calls) == 1
    assert sink.deltas == [(1, "分析")]
    assert sink.terminal == ("failed", 1)


@pytest.mark.asyncio
async def test_complete_json_stream_second_invalid_output_notifies_sink_failure() -> None:
    sink = CaptureThinkingSink()
    client = FakeCompletions(
        [
            stream_chunks(content_chunks=["{}"], reasoning_chunks=[None]),
            stream_chunks(content_chunks=["{}"], reasoning_chunks=[None]),
        ]
    )
    adapter = TencentPlanAdapter(client=client, log_writer=_CaptureWriter(), stream_support_cache={})

    with pytest.raises(ModelPlanInvalidError):
        await adapter.complete_json(_request(thinking_sink=sink))

    assert sink.terminals == [("failed", 1), ("failed", 2)]
