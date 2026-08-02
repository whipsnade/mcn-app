"""统一模型决策网关：思考流、动作协议解析与 Step 审计（Task 6）。

覆盖 spec §10.5：
- thinking 只来自供应商暴露的 reasoning_content / <think>，无思考时不发任何
  thinking.* 事件；
- 严格 JSON 动作只解析 think 结束后的 JSON，thinking 不参与动作校验；
- thinking 脱敏后写入 agent_steps.thinking_text，单次上限 64 KiB；
- Reviewer/Utility 内部 Run 的 thinking 只写 internal Step 审计。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.agent_runtime.model_gateway import MAX_THINKING_TEXT_CHARS, AgentModelGateway
from app.agent_runtime.models import AgentRun, AgentRunAttempt, AgentSession, AgentStep
from app.agent_runtime.profiles import PROFILES
from app.agent_runtime.prompts import get_system_prompt
from app.agent_runtime.repository import AgentRunRepository
from app.model.contracts import ChatMessage, ModelPlanInvalidError
from app.model.prompt_logs import PromptLogEntry
from app.model.tencent_plan import TencentPlanAdapter


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


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


def stream_chunks(
    *,
    content_chunks: list[str | None],
    reasoning_chunks: list[str | None],
) -> Any:
    """生成带最终 usage 块和 finish_reason=stop 的流式响应。"""
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


async def _get_step(db_session, run_id: str) -> AgentStep | None:
    return await db_session.scalar(select(AgentStep).where(AgentStep.run_id == run_id))


async def _create_run(
    db_session, user_factory, *, visibility: str = "user"
) -> tuple[AgentRun, AgentRunAttempt]:
    user = await user_factory()
    now = utc_now()
    session = AgentSession(
        id=str(uuid4()),
        user_id=user.id,
        title="网关测试会话",
        status="active",
        created_at=now,
        updated_at=now,
    )
    db_session.add(session)
    await db_session.flush()
    run = AgentRun(
        id=str(uuid4()),
        session_id=session.id,
        user_id=user.id,
        run_kind=visibility,
        visibility=visibility,
        profile_name="session_analyst_v1",
        profile_version="v1",
        model="test-model",
        status="queued",
        decision_count=0,
        review_count=0,
        revision_count=0,
    )
    db_session.add(run)
    await db_session.flush()
    attempt = await AgentRunRepository(db_session).begin_attempt(run.id)
    return run, attempt


def _make_gateway(db_session, outcomes: list[Any]) -> AgentModelGateway:
    adapter = TencentPlanAdapter(
        client=FakeCompletions(outcomes),
        log_writer=_CaptureWriter(),
        stream_support_cache={},
    )
    return AgentModelGateway(adapter, db=db_session)


_COMPLETE = '{"action":"complete","text":"done"}'
_PROFILE = PROFILES["session_analyst_v1"]


async def test_decide_streams_thinking_and_validates_only_json_tail(
    db_session, user_factory
) -> None:
    run, attempt = await _create_run(db_session, user_factory)
    sink = CaptureThinkingSink()
    # reasoning 内塞入损坏 JSON，不得影响最终动作解析（spec §10.5）。
    gateway = _make_gateway(
        db_session,
        [
            stream_chunks(
                content_chunks=[None, None, _COMPLETE],
                reasoning_chunks=["not-json{", "unclosed", None],
            )
        ],
    )

    action = await gateway.decide(
        run=run,
        attempt_id=attempt.id,
        profile=_PROFILE,
        messages=[ChatMessage(role="user", content="hi")],
        thinking_sink=sink,
        step_sequence=1,
    )

    assert type(action).__name__ == "Complete"
    assert action.action == "complete"
    assert action.text == "done"
    assert sink.started_attempts == [1]
    assert sink.deltas == [(1, "not-json{"), (1, "unclosed")]
    assert sink.terminal == ("completed", 1)


async def test_decide_without_thinking_emits_no_thinking_events(
    db_session, user_factory
) -> None:
    run, attempt = await _create_run(db_session, user_factory)
    sink = CaptureThinkingSink()
    gateway = _make_gateway(
        db_session,
        [stream_chunks(content_chunks=[_COMPLETE], reasoning_chunks=[None])],
    )

    action = await gateway.decide(
        run=run,
        attempt_id=attempt.id,
        profile=_PROFILE,
        messages=[ChatMessage(role="user", content="hi")],
        thinking_sink=sink,
        step_sequence=1,
    )

    assert action.action == "complete"
    # spec §10.5：供应商无 reasoning_content / <think> 时后端不发任何 thinking.* 事件。
    assert sink.started_attempts == []
    assert sink.deltas == []
    assert sink.terminal is None
    step = await _get_step(db_session, run.id)
    assert step is not None
    assert step.thinking_text is None


async def test_decide_repairs_broken_json_tail(db_session, user_factory) -> None:
    run, attempt = await _create_run(db_session, user_factory)
    broken = '{"action":"complete","text":"it\'s a "great" day"}'
    gateway = _make_gateway(
        db_session, [stream_chunks(content_chunks=[broken], reasoning_chunks=[None])]
    )

    action = await gateway.decide(
        run=run,
        attempt_id=attempt.id,
        profile=_PROFILE,
        messages=[ChatMessage(role="user", content="hi")],
        thinking_sink=None,
        step_sequence=1,
    )

    assert action.action == "complete"
    assert action.text == 'it\'s a "great" day'


async def test_decide_unrepairable_json_raises_adapter_error(
    db_session, user_factory
) -> None:
    run, attempt = await _create_run(db_session, user_factory)
    truncated = '{"action":"complete","text":"unterminated'
    gateway = _make_gateway(
        db_session,
        [
            stream_chunks(content_chunks=[truncated], reasoning_chunks=[None]),
            stream_chunks(content_chunks=[truncated], reasoning_chunks=[None]),
        ],
    )

    with pytest.raises(ModelPlanInvalidError):
        await gateway.decide(
            run=run,
            attempt_id=attempt.id,
            profile=_PROFILE,
            messages=[ChatMessage(role="user", content="hi")],
            thinking_sink=None,
            step_sequence=1,
        )

    step = await _get_step(db_session, run.id)
    assert step is not None
    assert step.status == "failed"


async def test_decide_persists_audit_step(db_session, user_factory) -> None:
    run, attempt = await _create_run(db_session, user_factory)
    gateway = _make_gateway(
        db_session,
        [
            stream_chunks(
                content_chunks=[None, _COMPLETE],
                reasoning_chunks=["分析品牌", None],
            )
        ],
    )

    action = await gateway.decide(
        run=run,
        attempt_id=attempt.id,
        profile=_PROFILE,
        messages=[ChatMessage(role="user", content="hi")],
        thinking_sink=None,
        step_sequence=7,
    )

    step = await _get_step(db_session, run.id)
    assert step is not None
    assert step.run_id == run.id
    assert step.attempt_id == attempt.id
    assert step.sequence == 7
    assert step.step_type == "model_decision"
    assert step.status == "completed"
    assert step.visibility == "user"
    assert step.model_request_id == "req-stream"
    assert step.token_usage_json == {
        "prompt_tokens": 3,
        "completion_tokens": 2,
        "total_tokens": 5,
        "cached_tokens": None,
        "reasoning_tokens": None,
    }
    assert step.thinking_text == "分析品牌"
    assert step.input_json == [
        {"role": "system", "content": get_system_prompt(_PROFILE.system_prompt_key).text},
        {"role": "user", "content": "hi"},
    ]
    assert step.output_json == action.model_dump()


async def test_decide_step_transitions_running_to_completed(
    db_session, user_factory
) -> None:
    run, attempt = await _create_run(db_session, user_factory)
    release = asyncio.Event()

    async def gated_stream() -> Any:
        yield SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content=None, reasoning_content="先想"),
                    finish_reason=None,
                )
            ],
            usage=None,
            _request_id="req-stream",
        )
        await release.wait()
        yield SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content=_COMPLETE, reasoning_content=None),
                    finish_reason=None,
                )
            ],
            usage=None,
            _request_id="req-stream",
        )
        yield SimpleNamespace(
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

    gateway = _make_gateway(db_session, [gated_stream()])
    decision_task = asyncio.create_task(
        gateway.decide(
            run=run,
            attempt_id=attempt.id,
            profile=_PROFILE,
            messages=[ChatMessage(role="user", content="hi")],
            thinking_sink=None,
            step_sequence=1,
        )
    )

    running_step = None
    for _ in range(200):
        running_step = await _get_step(db_session, run.id)
        if running_step is not None and running_step.status == "running":
            break
        await asyncio.sleep(0.001)
    assert running_step is not None
    assert running_step.status == "running"
    assert running_step.thinking_text is None

    release.set()
    action = await decision_task
    assert action.action == "complete"

    step = await _get_step(db_session, run.id)
    assert step is not None
    assert step.status == "completed"


async def test_decide_truncates_thinking_text_to_64kib(db_session, user_factory) -> None:
    run, attempt = await _create_run(db_session, user_factory)
    huge = "x" * 80_000
    gateway = _make_gateway(
        db_session,
        [
            stream_chunks(
                content_chunks=[None, _COMPLETE],
                reasoning_chunks=[huge, None],
            )
        ],
    )

    await gateway.decide(
        run=run,
        attempt_id=attempt.id,
        profile=_PROFILE,
        messages=[ChatMessage(role="user", content="hi")],
        thinking_sink=None,
        step_sequence=1,
    )

    step = await _get_step(db_session, run.id)
    assert step is not None
    assert len(step.thinking_text or "") == MAX_THINKING_TEXT_CHARS
    assert step.thinking_text == "x" * MAX_THINKING_TEXT_CHARS


async def test_decide_sanitizes_thinking_text(db_session, user_factory) -> None:
    run, attempt = await _create_run(db_session, user_factory)
    gateway = _make_gateway(
        db_session,
        [
            stream_chunks(
                content_chunks=[None, _COMPLETE],
                reasoning_chunks=["secret sk-abc123def456", None],
            )
        ],
    )

    await gateway.decide(
        run=run,
        attempt_id=attempt.id,
        profile=_PROFILE,
        messages=[ChatMessage(role="user", content="hi")],
        thinking_sink=None,
        step_sequence=1,
    )

    step = await _get_step(db_session, run.id)
    assert step is not None
    assert step.thinking_text == "secret [REDACTED]"


async def test_decide_uses_injected_sanitizer(db_session, user_factory) -> None:
    run, attempt = await _create_run(db_session, user_factory)
    adapter = TencentPlanAdapter(
        client=FakeCompletions(
            [
                stream_chunks(
                    content_chunks=[None, _COMPLETE],
                    reasoning_chunks=["敏感内容", None],
                )
            ]
        ),
        log_writer=_CaptureWriter(),
        stream_support_cache={},
    )
    gateway = AgentModelGateway(
        adapter, db=db_session, sanitize_thinking=lambda text: "CLEANED"
    )

    await gateway.decide(
        run=run,
        attempt_id=attempt.id,
        profile=_PROFILE,
        messages=[ChatMessage(role="user", content="hi")],
        thinking_sink=None,
        step_sequence=1,
    )

    step = await _get_step(db_session, run.id)
    assert step is not None
    assert step.thinking_text == "CLEANED"


async def test_decide_internal_run_audits_thinking_as_internal(
    db_session, user_factory
) -> None:
    run, attempt = await _create_run(db_session, user_factory, visibility="internal")
    gateway = _make_gateway(
        db_session,
        [
            stream_chunks(
                content_chunks=[None, _COMPLETE],
                reasoning_chunks=["内部思考", None],
            )
        ],
    )
    # 内部 Run 不产生用户可见思考事件：调用方不传 thinking_sink，
    # 思考仍落库到 internal Step 审计。
    await gateway.decide(
        run=run,
        attempt_id=attempt.id,
        profile=_PROFILE,
        messages=[ChatMessage(role="user", content="hi")],
        thinking_sink=None,
        step_sequence=1,
    )

    step = await _get_step(db_session, run.id)
    assert step is not None
    assert step.visibility == "internal"
    assert step.thinking_text == "内部思考"


async def test_decide_user_run_marks_step_user_visibility(
    db_session, user_factory
) -> None:
    run, attempt = await _create_run(db_session, user_factory, visibility="user")
    gateway = _make_gateway(
        db_session,
        [stream_chunks(content_chunks=[_COMPLETE], reasoning_chunks=[None])],
    )

    await gateway.decide(
        run=run,
        attempt_id=attempt.id,
        profile=_PROFILE,
        messages=[ChatMessage(role="user", content="hi")],
        thinking_sink=None,
        step_sequence=1,
    )

    step = await _get_step(db_session, run.id)
    assert step is not None
    assert step.visibility == "user"
