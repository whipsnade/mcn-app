"""AgentEventThinkingSink 单元测试（设计 §5.8 / §10.5）。

把模型网关转发的真实 thinking 回调持久化为 ``thinking.*`` 事件：
- started/delta/completed/failed 按序落 ``agent_events``（payload 带 attempt）；
- delta 文本逐条脱敏（与 prompt 日志/step 审计同一 redact 约束）；
- 累计 delta 超过 64 KiB 后丢弃后续 delta（completed/failed 终态仍发）；
- sink 只由执行层注入用户可见 Run（engine.thinking_sink_for 的可见性规则）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select

from app.agent_runtime.events import AgentEventBroker, AgentEventStream
from app.agent_runtime.model_gateway import MAX_THINKING_TEXT_CHARS
from app.agent_runtime.models import AgentEvent, AgentRun, AgentSession
from app.agent_runtime.thinking import AgentEventThinkingSink


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _create_run(db_session, user_factory) -> tuple[str, str]:
    user = await user_factory()
    now = utc_now()
    session = AgentSession(
        id=str(uuid4()),
        user_id=user.id,
        title="thinking sink 测试会话",
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
        run_kind="user",
        visibility="user",
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
    return user.id, run.id


async def _events(db_session, run_id: str) -> list[AgentEvent]:
    return list(
        (
            await db_session.scalars(
                select(AgentEvent)
                .where(AgentEvent.run_id == run_id)
                .order_by(AgentEvent.sequence)
            )
        ).all()
    )


def _make_sink(db_session, user_id: str, run_id: str) -> AgentEventThinkingSink:
    return AgentEventThinkingSink(
        AgentEventStream(db_session, AgentEventBroker()),
        run_id=run_id,
        user_id=user_id,
    )


async def test_sink_persists_started_delta_completed_in_order(
    db_session, user_factory
) -> None:
    user_id, run_id = await _create_run(db_session, user_factory)
    sink = _make_sink(db_session, user_id, run_id)

    await sink.started(attempt=1)
    await sink.delta("先分析", attempt=1)
    await sink.delta("再计算", attempt=1)
    await sink.completed(attempt=1, duration_ms=123)

    rows = await _events(db_session, run_id)
    assert [row.event_type for row in rows] == [
        "thinking.started",
        "thinking.delta",
        "thinking.delta",
        "thinking.completed",
    ]
    assert rows[0].payload_json["attempt"] == 1
    assert rows[1].payload_json["text"] == "先分析"
    assert rows[2].payload_json["text"] == "再计算"
    assert rows[3].payload_json["duration_ms"] == 123
    # 服务端 run_id 强制写入 payload
    assert all(row.payload_json["run_id"] == run_id for row in rows)


async def test_sink_persists_failed_event(db_session, user_factory) -> None:
    user_id, run_id = await _create_run(db_session, user_factory)
    sink = _make_sink(db_session, user_id, run_id)

    await sink.started(attempt=1)
    await sink.delta("半截思考", attempt=1)
    await sink.failed(attempt=1, error_code="MODEL_PLAN_INVALID")

    rows = await _events(db_session, run_id)
    assert [row.event_type for row in rows] == [
        "thinking.started",
        "thinking.delta",
        "thinking.failed",
    ]
    assert rows[-1].payload_json["error_code"] == "MODEL_PLAN_INVALID"


async def test_sink_sanitizes_delta_text(db_session, user_factory) -> None:
    user_id, run_id = await _create_run(db_session, user_factory)
    sink = _make_sink(db_session, user_id, run_id)

    await sink.delta("token 是 sk-abc123def456 对吧", attempt=1)

    rows = await _events(db_session, run_id)
    assert len(rows) == 1
    assert "sk-abc123def456" not in rows[0].payload_json["text"]
    assert "[REDACTED]" in rows[0].payload_json["text"]


async def test_sink_caps_cumulative_delta_at_64_kib(db_session, user_factory) -> None:
    """累计 delta 超 64 KiB 后丢弃后续 delta；completed 终态事件仍发出。"""
    user_id, run_id = await _create_run(db_session, user_factory)
    sink = _make_sink(db_session, user_id, run_id)

    await sink.delta("x" * MAX_THINKING_TEXT_CHARS, attempt=1)
    await sink.delta("y" * 100, attempt=1)  # 已达上限：整条丢弃
    await sink.completed(attempt=1, duration_ms=1)

    rows = await _events(db_session, run_id)
    types = [row.event_type for row in rows]
    assert types == ["thinking.delta", "thinking.completed"]
    emitted = sum(
        len(row.payload_json["text"]) for row in rows if row.event_type == "thinking.delta"
    )
    assert emitted == MAX_THINKING_TEXT_CHARS


async def test_sink_truncates_oversized_single_delta(db_session, user_factory) -> None:
    user_id, run_id = await _create_run(db_session, user_factory)
    sink = _make_sink(db_session, user_id, run_id)

    await sink.delta("x" * (MAX_THINKING_TEXT_CHARS + 500), attempt=1)

    rows = await _events(db_session, run_id)
    assert len(rows) == 1
    assert len(rows[0].payload_json["text"]) == MAX_THINKING_TEXT_CHARS


async def test_sink_ignores_empty_delta(db_session, user_factory) -> None:
    user_id, run_id = await _create_run(db_session, user_factory)
    sink = _make_sink(db_session, user_id, run_id)

    await sink.delta("", attempt=1)

    assert await _events(db_session, run_id) == []
