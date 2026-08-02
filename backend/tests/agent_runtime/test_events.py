import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from app.agent_runtime.events import (
    AgentEventBroker,
    AgentEventStream,
    RunEventForbidden,
)
from app.agent_runtime.models import AgentEvent, AgentRun, AgentSession
from app.db.session import SessionFactory, engine
from app.identity.models import User


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _create_run(db_session, user_factory) -> tuple[str, str]:
    user = await user_factory()
    now = utc_now()
    session = AgentSession(
        id=str(uuid4()),
        user_id=user.id,
        title="agent runtime 测试会话",
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
        profile_name="kol_analyst_v1",
        profile_version="1",
        model="test-model",
        status="queued",
        decision_count=0,
        review_count=0,
        revision_count=0,
    )
    db_session.add(run)
    await db_session.flush()
    return user.id, run.id


async def _create_committed_run() -> tuple[str, str, str]:
    """在独立事务中提交 user/session/run，供跨会话可见性测试使用。"""
    async with SessionFactory() as db:
        now = utc_now()
        user = User(
            id=str(uuid4()),
            nickname="测试用户",
            role="user",
            status="active",
            created_at=now,
            updated_at=now,
        )
        db.add(user)
        await db.flush()
        session = AgentSession(
            id=str(uuid4()),
            user_id=user.id,
            title="agent runtime 测试会话",
            status="active",
            created_at=now,
            updated_at=now,
        )
        db.add(session)
        await db.flush()
        run = AgentRun(
            id=str(uuid4()),
            session_id=session.id,
            user_id=user.id,
            run_kind="user",
            visibility="user",
            profile_name="kol_analyst_v1",
            profile_version="1",
            model="test-model",
            status="queued",
            decision_count=0,
            review_count=0,
            revision_count=0,
        )
        db.add(run)
        await db.flush()
        await db.commit()
        return user.id, session.id, run.id


async def _purge_committed(user_id: str, session_id: str, run_id: str) -> None:
    """删除独立会话测试提交的行，保持测试库干净。"""
    async with engine.begin() as conn:
        await conn.execute(delete(AgentEvent).where(AgentEvent.run_id == run_id))
        await conn.execute(delete(AgentRun).where(AgentRun.id == run_id))
        await conn.execute(delete(AgentSession).where(AgentSession.id == session_id))
        await conn.execute(delete(User).where(User.id == user_id))


async def test_append_produces_contiguous_sequences_for_a_run(
    db_session, user_factory
) -> None:
    user_id, run_id = await _create_run(db_session, user_factory)
    stream = AgentEventStream(db_session, AgentEventBroker())

    appended = [
        await stream.append(run_id, user_id, "thinking.delta", {"text": f"chunk {index}"})
        for index in range(5)
    ]

    assert [event.sequence for event in appended] == [1, 2, 3, 4, 5]
    rows = list(
        (
            await db_session.scalars(
                select(AgentEvent)
                .where(AgentEvent.run_id == run_id)
                .order_by(AgentEvent.sequence)
            )
        ).all()
    )
    assert [row.sequence for row in rows] == [1, 2, 3, 4, 5]
    assert rows[0].user_id == user_id
    # spec §15.3：事件 payload 必须带 run_id
    assert rows[0].payload_json["run_id"] == run_id


async def test_append_forces_server_run_id_in_payload(db_session, user_factory) -> None:
    user_id, run_id = await _create_run(db_session, user_factory)
    stream = AgentEventStream(db_session, AgentEventBroker())

    event = await stream.append(
        run_id, user_id, "tool.started", {"run_id": "spoofed", "tool": "x"}
    )

    # 服务端 run_id 强制覆盖，客户端不可伪造
    assert event.payload_json["run_id"] == run_id
    row = await db_session.scalar(select(AgentEvent).where(AgentEvent.run_id == run_id))
    assert row.payload_json["run_id"] == run_id
    assert row.payload_json["tool"] == "x"


async def test_append_rejects_non_owner(db_session, user_factory) -> None:
    _owner_id, run_id = await _create_run(db_session, user_factory)
    other_user = await user_factory()
    stream = AgentEventStream(db_session, AgentEventBroker())

    with pytest.raises(RunEventForbidden):
        await stream.append(run_id, other_user.id, "thinking.delta", {"text": "x"})

    rows = list(
        (await db_session.scalars(select(AgentEvent).where(AgentEvent.run_id == run_id))).all()
    )
    assert rows == []


async def test_append_unknown_run_raises(db_session, user_factory) -> None:
    user = await user_factory()
    stream = AgentEventStream(db_session, AgentEventBroker())

    with pytest.raises(RunEventForbidden):
        await stream.append(str(uuid4()), user.id, "thinking.delta", {})


async def test_sequences_are_per_run_not_global(db_session, user_factory) -> None:
    stream = AgentEventStream(db_session, AgentEventBroker())
    user_a, run_a = await _create_run(db_session, user_factory)
    user_b, run_b = await _create_run(db_session, user_factory)

    for index in range(3):
        await stream.append(run_a, user_a, "thinking.delta", {"index": index})
    await stream.append(run_b, user_b, "run.started", {})
    await stream.append(run_b, user_b, "message.completed", {})

    rows_a = list(
        (
            await db_session.scalars(
                select(AgentEvent)
                .where(AgentEvent.run_id == run_a)
                .order_by(AgentEvent.sequence)
            )
        ).all()
    )
    rows_b = list(
        (
            await db_session.scalars(
                select(AgentEvent)
                .where(AgentEvent.run_id == run_b)
                .order_by(AgentEvent.sequence)
            )
        ).all()
    )
    assert [row.sequence for row in rows_a] == [1, 2, 3]
    assert [row.sequence for row in rows_b] == [1, 2]


async def test_stream_replays_only_events_after_last_event_id(
    db_session, user_factory
) -> None:
    user_id, run_id = await _create_run(db_session, user_factory)
    stream = AgentEventStream(db_session, AgentEventBroker())
    await stream.append(run_id, user_id, "thinking.delta", {"text": "a"})
    await stream.append(run_id, user_id, "thinking.delta", {"text": "b"})
    await stream.append(run_id, user_id, "run.completed", {})

    events = [event async for event in stream.stream(run_id, user_id, last_event_id=1)]

    assert [event.sequence for event in events] == [2, 3]
    assert [event.event_type for event in events] == ["thinking.delta", "run.completed"]


async def test_stream_rejects_non_owner(db_session, user_factory) -> None:
    _owner_id, run_id = await _create_run(db_session, user_factory)
    other_user = await user_factory()
    stream = AgentEventStream(db_session, AgentEventBroker())

    with pytest.raises(RunEventForbidden):
        async for _ in stream.stream(run_id, other_user.id, last_event_id=0):
            pass


async def test_stream_unknown_run_raises(db_session, user_factory) -> None:
    user = await user_factory()
    stream = AgentEventStream(db_session, AgentEventBroker())

    with pytest.raises(RunEventForbidden):
        async for _ in stream.stream(str(uuid4()), user.id, last_event_id=0):
            pass


async def test_stream_ends_after_terminal_event(db_session, user_factory) -> None:
    user_id, run_id = await _create_run(db_session, user_factory)
    stream = AgentEventStream(db_session, AgentEventBroker())
    await stream.append(run_id, user_id, "run.started", {})
    await stream.append(run_id, user_id, "run.completed", {})

    events = [event async for event in stream.stream(run_id, user_id, last_event_id=0)]

    assert [event.event_type for event in events] == ["run.started", "run.completed"]
    assert events[-1].sequence == 2


async def test_stream_delivers_live_appends_via_broker() -> None:
    """同进程 append 经 broker 秒达；reader 与 writer 各用独立会话（生产形态）。"""
    broker = AgentEventBroker()
    user_id, session_id, run_id = await _create_committed_run()
    try:
        async with SessionFactory() as writer0:
            await AgentEventStream(writer0, broker).append(run_id, user_id, "run.started", {})

        async with SessionFactory() as reader:
            stream_b = AgentEventStream(reader, broker)

            async def consumer() -> list[int]:
                collected = []
                async for event in stream_b.stream(run_id, user_id, last_event_id=0):
                    collected.append(event.sequence)
                    if event.event_type == "run.completed":
                        return collected
                return collected

            task = asyncio.create_task(consumer())
            await asyncio.sleep(0.05)  # reader 重放 seq1 并进入 broker 等待

            async with SessionFactory() as writer:
                stream_a = AgentEventStream(writer, broker)
                await stream_a.append(run_id, user_id, "thinking.delta", {"text": "live"})
                await stream_a.append(run_id, user_id, "run.completed", {})

            result = await asyncio.wait_for(task, timeout=5)
    finally:
        await _purge_committed(user_id, session_id, run_id)

    assert result == [1, 2, 3]


async def test_stream_polls_committed_events_from_other_session() -> None:
    """跨进程：writer 用独立 broker，reader 只能靠 DB 轮询发现已提交事件。

    若 stream 复用固定快照（Fix 1 回归），reader 永远看不到跨会话提交的事件，
    终态事件无法结束流，本测试会等到 wait_for 超时失败。
    """
    broker = AgentEventBroker()
    user_id, session_id, run_id = await _create_committed_run()
    try:
        async with SessionFactory() as reader:
            stream_b = AgentEventStream(reader, broker)

            async def consumer() -> list[str]:
                collected = []
                async for event in stream_b.stream(run_id, user_id, last_event_id=0):
                    collected.append(event.event_type)
                    if event.event_type == "run.completed":
                        return collected
                return collected

            task = asyncio.create_task(consumer())
            await asyncio.sleep(0.05)  # reader 订阅并进入空闲轮询

            # 独立进程：独立 broker + 独立会话，reader 的 broker 收不到任何唤醒
            async with SessionFactory() as writer:
                stream_a = AgentEventStream(writer, AgentEventBroker())
                await stream_a.append(run_id, user_id, "run.started", {})
                await stream_a.append(run_id, user_id, "thinking.delta", {"text": "cross"})
                await stream_a.append(run_id, user_id, "run.completed", {})

            result = await asyncio.wait_for(task, timeout=5)
    finally:
        await _purge_committed(user_id, session_id, run_id)

    assert result == ["run.started", "thinking.delta", "run.completed"]
