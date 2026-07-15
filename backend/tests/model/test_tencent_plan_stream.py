import asyncio
from unittest.mock import AsyncMock

import pytest

from app.model.contracts import (
    ChatMessage,
    ModelStreamInterrupted,
    StreamingModelRequest,
)
from app.model.tencent_plan import TencentPlanAdapter
from .fakes import (
    FakeChatCompletions,
    ScriptedStream,
    adapter_with_broken_stream,
    adapter_with_stream,
    chunk,
    connection_error,
    usage_chunk,
)


def summary_request() -> StreamingModelRequest:
    return StreamingModelRequest(
        messages=(ChatMessage(role="user", content="总结"),),
    )


@pytest.mark.asyncio
async def test_stream_skips_empty_delta_and_emits_usage_then_done() -> None:
    adapter, _ = adapter_with_stream(
        chunk(content="", finish_reason=None),
        chunk(content="推荐", finish_reason=None),
        chunk(content=None, finish_reason="stop"),
        usage_chunk(prompt=8, completion=2, total=10),
    )

    events = [event async for event in adapter.stream_text(summary_request())]

    assert [event.type for event in events] == [
        "text.delta",
        "usage.updated",
        "stream.completed",
    ]
    assert events[0].text == "推荐"
    assert events[1].usage is not None and events[1].usage.total_tokens == 10
    assert events[2].finish_reason == "stop"


@pytest.mark.asyncio
async def test_partial_stream_interruption_is_not_retried() -> None:
    adapter, client = adapter_with_broken_stream(first_text="部分结论")
    received = []

    with pytest.raises(ModelStreamInterrupted) as caught:
        async for event in adapter.stream_text(summary_request()):
            received.append(event)

    assert received[0].text == "部分结论"
    assert caught.value.partial_output_received is True
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_stream_retries_transient_error_before_body() -> None:
    client = FakeChatCompletions(
        [
            ScriptedStream((connection_error(),)),
            ScriptedStream((chunk(content="完整", finish_reason="stop"),)),
        ]
    )
    adapter = TencentPlanAdapter(client=client, sleep=AsyncMock(), jitter=lambda: 0.0)

    events = [event async for event in adapter.stream_text(summary_request())]

    assert events[0].text == "完整"
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_stream_eof_without_finish_reason_is_interrupted() -> None:
    adapter, client = adapter_with_stream(chunk(content=None, finish_reason=None))

    with pytest.raises(ModelStreamInterrupted) as caught:
        _ = [event async for event in adapter.stream_text(summary_request())]

    assert caught.value.partial_output_received is False
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_stream_usage_fields_may_all_be_missing() -> None:
    adapter, _ = adapter_with_stream(
        chunk(content=None, finish_reason="stop"),
        usage_chunk(prompt=None, completion=None, total=None),
    )

    events = [event async for event in adapter.stream_text(summary_request())]

    assert events[0].usage is not None
    assert events[0].usage.model_dump() == {
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "cached_tokens": None,
        "reasoning_tokens": None,
    }


@pytest.mark.asyncio
async def test_cancelled_error_propagates_unchanged() -> None:
    client = FakeChatCompletions([ScriptedStream((asyncio.CancelledError(),))])
    adapter = TencentPlanAdapter(client=client, sleep=AsyncMock())

    with pytest.raises(asyncio.CancelledError):
        _ = [event async for event in adapter.stream_text(summary_request())]

    assert len(client.calls) == 1
