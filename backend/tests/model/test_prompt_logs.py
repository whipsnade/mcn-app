"""prompt 学习日志：适配器统一出口的三种状态判定与 log_context 透传。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import delete, select

from app.brainstorm.schemas import BrainstormModelOutput
from app.db.session import SessionFactory
from app.goals.schemas import GoalPlannerOutput
from app.model.contracts import (
    ChatMessage,
    ModelPlanInvalidError,
    StreamingModelRequest,
    StructuredModelRequest,
)
from app.model.models import ModelPromptLog
from app.model.prompt_logs import PromptLogEntry, record_prompt_log
from app.model.tencent_plan import TencentPlanAdapter
from app.orchestration.loop import AgentDecision
from app.reporting.blocks import ReportDocument


MINIMAX_THINK_RESPONSE = (
    "<think>检查当前画像，确认品牌、品类和平台是否齐全。</think>\n"
    '{"assistant_message":"请确认品类","extracted":{"audience":null,'
    '"brand":"Manner","category":null,"goal":"声量和情感趋势",'
    '"kol_filters":null,"period":null,"platforms":[],"region":null},'
    '"question":{"options":["咖啡/现制饮品"],"text":"请选择品类"},'
    '"ready":false,"title_suggestion":"Manner品牌分析"}'
)


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


def _structured_stream(content: str) -> Any:
    chunks = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content=content, reasoning_content=None),
                    finish_reason=None,
                )
            ],
            usage=None,
            _request_id="req-structured",
        ),
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content=None, reasoning_content=None),
                    finish_reason="stop",
                )
            ],
            usage=None,
            _request_id="req-structured",
        ),
    ]

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


class _FailingThinkingSink:
    async def started(self, *, attempt: int) -> None:
        raise RuntimeError("sink down")

    async def delta(self, text: str, *, attempt: int) -> None:
        raise RuntimeError("sink down")

    async def completed(self, *, attempt: int, duration_ms: int) -> None:
        raise RuntimeError("sink down")

    async def failed(self, *, attempt: int, error_code: str) -> None:
        raise RuntimeError("sink down")


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
async def test_complete_json_accepts_minimax_think_response_and_keeps_raw_log() -> None:
    writer = _CaptureWriter()
    adapter = TencentPlanAdapter(
        client=_FakeCompletions([_json_response(MINIMAX_THINK_RESPONSE)]),
        log_writer=writer,
    )

    result = await adapter.complete_json(
        StructuredModelRequest(
            purpose="brainstorm",
            template_name="brainstorm_v1",
            messages=(ChatMessage(role="user", content="分析品牌"),),
            output_model=BrainstormModelOutput,
        )
    )

    assert result.value.assistant_message == "请确认品类"
    assert result.value.extracted.brand == "Manner"
    assert writer.entries[0].status == "success"
    assert writer.entries[0].response == MINIMAX_THINK_RESPONSE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("purpose", "output_model", "response", "expected"),
    [
        (
            "brainstorm",
            BrainstormModelOutput,
            MINIMAX_THINK_RESPONSE,
            ("assistant_message", "请确认品类"),
        ),
        (
            "goal_planner",
            GoalPlannerOutput,
            (
                '{"action":"execute","question":null,"goals":[{"sequence":1,'
                '"goal_type":"brand_analysis","depends_on_sequence":null,'
                '"params":{"brand":"Manner","campaign":null,"period":null,'
                '"platforms":[],"requirement":"分析品牌声量"},'
                '"request_evidence":"分析 Manner 品牌声量"}],'
                '"active_brand":"Manner","brand_source":"explicit"}'
            ),
            ("action", "execute"),
        ),
        (
            "agent_loop",
            AgentDecision,
            (
                '{"action":"finish","internal_tool_name":null,"arguments":{},'
                '"evidence_goal":"","rationale":"证据足够","conclusion":"分析完成"}'
            ),
            ("conclusion", "分析完成"),
        ),
        (
            "brand_analysis",
            ReportDocument,
            (
                '{"title":"品牌分析","conclusion":"趋势稳定",'
                '"blocks":[{"type":"markdown","text":"报告已生成"}]}'
            ),
            ("title", "品牌分析"),
        ),
    ],
)
async def test_thinking_sink_failure_does_not_break_user_visible_structured_flows(
    purpose, output_model, response: str, expected: tuple[str, str]
) -> None:
    writer = _CaptureWriter()
    adapter = TencentPlanAdapter(
        client=_FakeCompletions([_structured_stream(response)]),
        log_writer=writer,
        stream_support_cache={},
    )

    result = await adapter.complete_json(
        StructuredModelRequest(
            purpose=purpose,
            template_name=f"{purpose}_v1",
            messages=(ChatMessage(role="user", content="执行分析"),),
            output_model=output_model,
            thinking_sink=_FailingThinkingSink(),
        )
    )

    assert getattr(result.value, expected[0]) == expected[1]
    assert writer.entries[0].status == "success"


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
