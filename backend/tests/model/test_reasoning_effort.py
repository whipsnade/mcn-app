"""reasoning_effort（思考深度）配置：配置了才发送，未配置不发送。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from app.model.contracts import ChatMessage, StreamingModelRequest, StructuredModelRequest
from app.model.tencent_plan import TencentPlanAdapter


class _Out(BaseModel):
    value: int


def _json_request() -> StructuredModelRequest[_Out]:
    return StructuredModelRequest(
        purpose="agent_loop",
        template_name="agent_loop_v1",
        messages=(
            ChatMessage(role="system", content="sys"),
            ChatMessage(role="user", content="user"),
        ),
        output_model=_Out,
    )


def _stream_request() -> StreamingModelRequest:
    return StreamingModelRequest(
        messages=(ChatMessage(role="user", content="user"),),
    )


class _FakeCompletions:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.calls: list[dict] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.outcome


def _json_response(content: str) -> Any:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason="stop")],
        usage=None,
        _request_id="req-test",
    )


def _stream_chunks() -> Any:
    chunks = [
        SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content=None), finish_reason="stop")],
            usage=None,
        )
    ]

    async def stream():
        for chunk in chunks:
            yield chunk

    return stream()


@pytest.mark.asyncio
async def test_reasoning_effort_sent_when_configured() -> None:
    client = _FakeCompletions(_json_response('{"value": 1}'))
    adapter = TencentPlanAdapter(client=client, reasoning_effort="high")

    await adapter.complete_json(_json_request())

    assert client.calls[0]["reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_reasoning_effort_omitted_when_not_configured() -> None:
    client = _FakeCompletions(_json_response('{"value": 1}'))
    adapter = TencentPlanAdapter(client=client)

    await adapter.complete_json(_json_request())

    assert "reasoning_effort" not in client.calls[0]


@pytest.mark.asyncio
async def test_reasoning_effort_sent_on_stream_when_configured() -> None:
    client = _FakeCompletions(_stream_chunks())
    adapter = TencentPlanAdapter(client=client, reasoning_effort="max")

    async for _event in adapter.stream_text(_stream_request()):
        pass

    assert client.calls[0]["reasoning_effort"] == "max"
