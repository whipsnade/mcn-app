"""prompt 学习日志：适配器统一出口的三种状态判定与 log_context 透传。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import delete, select

from app.db.session import SessionFactory
from app.model.contracts import (
    ChatMessage,
    ModelPlanInvalidError,
    StreamingModelRequest,
    StructuredModelRequest,
)
from app.model.models import ModelPromptLog
from app.model.prompt_logs import PromptLogEntry, record_prompt_log
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
        "log_context": {"user_id": "u-1", "session_id": "s-1", "tags": ["platform:douyin"]},
    }
    values.update(overrides)
    return StructuredModelRequest(**values)


class _FakeCompletions:
    """脚本化 chat.completions：outcomes 依次消费，异常直接抛出。"""

    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _json_response(content: str) -> Any:
    usage = SimpleNamespace(
        prompt_tokens=11,
        completion_tokens=7,
        total_tokens=18,
        prompt_tokens_details=None,
        completion_details=None,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason="stop")],
        usage=usage,
        _request_id="req-test",
    )


def _stream_chunks(*texts: str) -> Any:
    chunks = [
        SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content=text), finish_reason=None)],
            usage=None,
        )
        for text in texts
    ]
    chunks.append(
        SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content=None), finish_reason="stop")],
            usage=SimpleNamespace(
                prompt_tokens=3,
                completion_tokens=2,
                total_tokens=5,
                prompt_tokens_details=None,
                completion_details=None,
            ),
        )
    )

    async def stream():
        for chunk in chunks:
            yield chunk

    return stream()


class _CaptureWriter:
    def __init__(self, *, fail: bool = False) -> None:
        self.entries: list[PromptLogEntry] = []
        self._fail = fail

    async def __call__(self, entry: PromptLogEntry) -> None:
        if self._fail:
            raise RuntimeError("log store down")
        self.entries.append(entry)


@pytest.mark.asyncio
async def test_complete_json_success_logs_with_log_context() -> None:
    writer = _CaptureWriter()
    adapter = TencentPlanAdapter(
        client=_FakeCompletions([_json_response('{"value": 1}')]), log_writer=writer
    )

    result = await adapter.complete_json(_request())

    assert result.value.value == 1
    [entry] = writer.entries
    assert entry.status == "success"
    assert entry.purpose == "agent_loop"
    assert entry.user_id == "u-1"
    assert entry.session_id == "s-1"
    assert entry.tags == ("platform:douyin",)
    assert entry.prompt_tokens == 11
    assert entry.completion_tokens == 7
    assert entry.duration_ms is not None
    assert '{"value": 1}' in entry.response
    assert "user" in entry.messages


@pytest.mark.asyncio
async def test_complete_json_repair_still_invalid_logs_invalid() -> None:
    writer = _CaptureWriter()
    adapter = TencentPlanAdapter(
        client=_FakeCompletions([_json_response("not-json"), _json_response('{"value": "x"}')]),
        log_writer=writer,
    )

    with pytest.raises(ModelPlanInvalidError):
        await adapter.complete_json(_request())

    [entry] = writer.entries
    assert entry.status == "invalid"
    assert entry.error_code == "MODEL_PLAN_INVALID"
    assert entry.response == '{"value": "x"}'


@pytest.mark.asyncio
async def test_complete_json_adapter_error_logs_failed_with_error_code() -> None:
    writer = _CaptureWriter()
    adapter = TencentPlanAdapter(
        client=_FakeCompletions([RuntimeError("boom")]), log_writer=writer
    )

    with pytest.raises(Exception, match="MODEL_UPSTREAM_ERROR"):
        await adapter.complete_json(_request())

    [entry] = writer.entries
    assert entry.status == "failed"
    assert entry.error_code == "MODEL_UPSTREAM_ERROR"


@pytest.mark.asyncio
async def test_log_writer_failure_never_blocks_main_flow() -> None:
    adapter = TencentPlanAdapter(
        client=_FakeCompletions([_json_response('{"value": 2}')]),
        log_writer=_CaptureWriter(fail=True),
    )

    result = await adapter.complete_json(_request())

    assert result.value.value == 2


@pytest.mark.asyncio
async def test_stream_text_logs_success_with_usage() -> None:
    writer = _CaptureWriter()
    adapter = TencentPlanAdapter(
        client=_FakeCompletions([_stream_chunks("你好", "世界")]), log_writer=writer
    )
    request = StreamingModelRequest(
        messages=(ChatMessage(role="user", content="总结"),),
        log_context={"task_id": "t-1", "tags": ["summary"]},
    )

    texts = [
        event.text
        async for event in adapter.stream_text(request)
        if event.type == "text.delta"
    ]

    assert texts == ["你好", "世界"]
    [entry] = writer.entries
    assert entry.status == "success"
    assert entry.purpose == "summary"
    assert entry.task_id == "t-1"
    assert entry.response == "你好世界"
    assert entry.prompt_tokens == 3


@pytest.mark.asyncio
async def test_record_prompt_log_persists_row() -> None:
    entry = PromptLogEntry(
        purpose="quick_feature",
        model="deepseek-v4-pro",
        messages='[{"role": "user", "content": "{}"}]',
        response='{"action": "finish"}',
        status="success",
        tags=("quick:top_posts", "industry:美食"),
        duration_ms=42,
    )

    await record_prompt_log(entry)

    try:
        async with SessionFactory() as db:
            row = await db.scalar(
                select(ModelPromptLog).where(
                    ModelPromptLog.purpose == "quick_feature",
                    ModelPromptLog.duration_ms == 42,
                )
            )
        assert row is not None
        assert row.status == "success"
        assert row.tags == ["quick:top_posts", "industry:美食"]
        assert row.user_id is None  # 缺省上下文也必须落库
        assert row.response == '{"action": "finish"}'
    finally:
        async with SessionFactory.begin() as db:
            await db.execute(
                delete(ModelPromptLog).where(
                    ModelPromptLog.purpose == "quick_feature",
                    ModelPromptLog.duration_ms == 42,
                )
            )
