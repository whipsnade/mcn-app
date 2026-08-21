"""Run 终态收口事务边界 ``settle_terminal`` 测试（H1：恰好一次终态事件）。

契约：``AgentEventStream.settle_terminal`` 在同一加锁事务内完成
``SELECT ... FOR UPDATE`` 锁 Run 行 → 复核终态事件 → 迁移 Run 状态 →
插入终态事件（sequence=max+1）→ 提交。覆盖：

1. 三种终态（completed/failed/cancelled）的迁移 + 事件原子提交；
2. 幂等与租约闸门（已有终态事件 / 他人活跃持有租约时不重复收口）；
3. 并发：两个独立会话对同一 Run 并发收口 → 恰好一个终态事件、另一方
   幂等成功（不是 IntegrityError）；
4. 崩溃注入：终态事务中途异常 → 整体回滚，Run 保持非终态且无事件，
   恢复后可再次收口并成功发事件；
5. 旧窗口残留修复：Run 已终态但缺终态事件时按实际终态补发（不再迁移）。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.agent_runtime.events import (
    AgentEventBroker,
    AgentEventStream,
    is_terminal_event,
)
from app.agent_runtime.models import AgentEvent, AgentMessage, AgentRun, AgentSession
from app.agent_runtime.repository import AgentRunRepository
from app.agent_runtime.state import InvalidRunTransition, RunStatus
from app.db.session import SessionFactory
from app.pi_gateway.completion import CompletionValidator

from tests.agent_runtime.test_events import _create_committed_run, _purge_committed


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _make_run(
    db_session, user_factory, *, start: bool = False, worker: str | None = "worker"
) -> AgentRun:
    """构造 Run；``start=True`` 时开启 Attempt（running），``worker`` 非空时领取租约。"""
    user = await user_factory()
    now = utc_now()
    session = AgentSession(
        id=str(uuid4()),
        user_id=user.id,
        title="settle_terminal 测试会话",
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
    if start:
        db_session.add(
            AgentMessage(
                id=str(uuid4()),
                session_id=session.id,
                run_id=run.id,
                role="assistant",
                content="测试完成消息",
                metadata_json={"test_fixture": True},
                sequence=1,
                created_at=now,
            )
        )
        await db_session.flush()
        repo = AgentRunRepository(db_session)
        await repo.begin_attempt(run.id)
        if worker is not None:
            assert await repo.claim_lease(run.id, worker, 300)
    return run


async def _run_events(db_session, run_id: str) -> list[AgentEvent]:
    return list(
        (
            await db_session.scalars(
                select(AgentEvent)
                .where(AgentEvent.run_id == run_id)
                .order_by(AgentEvent.sequence)
            )
        ).all()
    )


async def _terminal_events(db_session, run_id: str) -> list[AgentEvent]:
    return [row for row in await _run_events(db_session, run_id) if is_terminal_event(row.event_type)]


async def _create_committed_running_run(worker: str = "worker-a") -> tuple[str, str, str]:
    """跨会话测试现场：已提交的 running Run + worker 持有的活跃租约。"""
    user_id, session_id, run_id = await _create_committed_run()
    async with SessionFactory() as db:
        repo = AgentRunRepository(db)
        await repo.begin_attempt(run_id)
        assert await repo.claim_lease(run_id, worker, 300)
        db.add(
            AgentMessage(
                id=str(uuid4()),
                session_id=session_id,
                run_id=run_id,
                role="assistant",
                content="并发测试完成消息",
                metadata_json={"test_fixture": True},
                sequence=1,
                created_at=utc_now(),
            )
        )
        await db.commit()
    return user_id, session_id, run_id


# ---------------------------------------------------------------------------
# 1. 三种终态的迁移 + 事件原子提交
# ---------------------------------------------------------------------------


async def test_settle_terminal_completed_migrates_and_emits_in_one_commit(
    db_session, user_factory
) -> None:
    """completed：持租约 worker 收口——迁移、Attempt 收尾、终态事件一体提交。"""
    run = await _make_run(db_session, user_factory, start=True, worker="worker")
    await AgentEventStream(db_session, AgentEventBroker()).append(
        run.id, run.user_id, "message.completed", {"type": "completion"}
    )
    stream = AgentEventStream(db_session, AgentEventBroker())

    event = await stream.settle_terminal(
        run.id,
        run.user_id,
        RunStatus.COMPLETED,
        {"outcome": "completed"},
        worker_id="worker",
    )

    assert event is not None
    assert event.event_type == "run.completed"
    assert event.payload_json["run_id"] == run.id
    fresh = await db_session.get(AgentRun, run.id)
    assert fresh.status == RunStatus.COMPLETED
    assert fresh.completed_at is not None
    assert fresh.lease_owner is None
    rows = await _run_events(db_session, run.id)
    assert [row.event_type for row in rows] == ["message.completed", "run.completed"]


async def test_settle_terminal_formal_pi_run_rejects_text_without_main_report(
    db_session, user_factory
) -> None:
    run = await _make_run(db_session, user_factory, start=True, worker="worker")
    run.runtime_backend = "pi"
    run.runtime_config_snapshot_json = {
        "runtime_backend": "pi",
        "profile_name": "session_analyst_v1",
        "allowed_artifact_contracts": ["analysis_report_v1"],
        "capability_pack": {
            "pack_version": "test-pack-v1",
            "manifest_digest": "test-manifest-digest",
        },
        "capability_pack_version": "test-pack-v1",
        "capability_pack_manifest_digest": "test-manifest-digest",
    }
    stream = AgentEventStream(db_session, AgentEventBroker())

    with pytest.raises(InvalidRunTransition, match="pi_gateway_main_artifact_missing"):
        await stream.settle_terminal(
            run.id,
            run.user_id,
            RunStatus.COMPLETED,
            {"outcome": "completed"},
            worker_id="worker",
        )

    fresh = await db_session.get(AgentRun, run.id)
    assert fresh.status == RunStatus.RUNNING
    assert await _terminal_events(db_session, run.id) == []


async def test_force_complete_uses_the_same_formal_main_report_validator(
    db_session, user_factory
) -> None:
    run = await _make_run(db_session, user_factory, start=True, worker=None)
    run.runtime_backend = "pi"
    run.runtime_config_snapshot_json = {
        "runtime_backend": "pi",
        "profile_name": "session_analyst_v1",
        "allowed_artifact_contracts": ["brand_report_v3", "analysis_report_v1"],
        "capability_pack": {
            "pack_version": "test-pack-v1",
            "manifest_digest": "test-manifest-digest",
        },
        "capability_pack_version": "test-pack-v1",
        "capability_pack_manifest_digest": "test-manifest-digest",
    }

    with pytest.raises(InvalidRunTransition, match="pi_gateway_main_artifact_missing"):
        await AgentRunRepository(db_session).force_complete(
            run.id,
            completion_validator=CompletionValidator(db_session).validate,
        )

    fresh = await db_session.get(AgentRun, run.id)
    assert fresh.status == RunStatus.RUNNING


async def test_settle_terminal_runs_gateway_cleanup_before_commit(db_session, user_factory) -> None:
    run = await _make_run(db_session, user_factory, start=True, worker="worker")
    seen: list[str] = []

    async def cleanup(locked_run: AgentRun) -> None:
        seen.append(locked_run.id)

    event = await AgentEventStream(db_session, AgentEventBroker()).settle_terminal(
        run.id,
        run.user_id,
        RunStatus.COMPLETED,
        {},
        worker_id="worker",
        before_commit=cleanup,
    )

    assert event is not None
    assert seen == [run.id]


async def test_settle_terminal_failed_with_lease_carries_error_code(
    db_session, user_factory
) -> None:
    """failed：持租约走状态机迁移，error_code 随事件落库。"""
    run = await _make_run(db_session, user_factory, start=True, worker="worker")
    stream = AgentEventStream(db_session, AgentEventBroker())

    event = await stream.settle_terminal(
        run.id,
        run.user_id,
        RunStatus.FAILED,
        {"outcome": "failed", "error_code": "model_error"},
        worker_id="worker",
    )

    assert event is not None
    assert event.payload_json["error_code"] == "model_error"
    fresh = await db_session.get(AgentRun, run.id)
    assert fresh.status == RunStatus.FAILED


async def test_settle_terminal_failed_without_lease_uses_force_fail(
    db_session, user_factory
) -> None:
    """failed + 无活跃租约（系统级收口）：force_fail 语义——无其他活跃持有者时迁移。"""
    run = await _make_run(db_session, user_factory, start=True, worker=None)
    stream = AgentEventStream(db_session, AgentEventBroker())

    event = await stream.settle_terminal(
        run.id,
        run.user_id,
        RunStatus.FAILED,
        {"outcome": "failed", "error_code": "executor_error"},
        worker_id="worker-x",
    )

    assert event is not None
    fresh = await db_session.get(AgentRun, run.id)
    assert fresh.status == RunStatus.FAILED
    assert fresh.error_code == "executor_error"


async def test_settle_terminal_cancelled_from_queued(db_session, user_factory) -> None:
    """cancelled：用户取消跨切面语义——queued 无租约也直接迁移（不看租约）。"""
    run = await _make_run(db_session, user_factory)
    stream = AgentEventStream(db_session, AgentEventBroker())

    event = await stream.settle_terminal(run.id, run.user_id, RunStatus.CANCELLED, {})

    assert event is not None
    assert event.event_type == "run.cancelled"
    fresh = await db_session.get(AgentRun, run.id)
    assert fresh.status == RunStatus.CANCELLED


async def test_settle_terminal_rejects_non_terminal_outcome(
    db_session, user_factory
) -> None:
    """非终态 outcome 是编排错误：抛 ValueError，不静默。"""
    run = await _make_run(db_session, user_factory, start=True)
    stream = AgentEventStream(db_session, AgentEventBroker())

    with pytest.raises(ValueError, match="not a terminal outcome"):
        await stream.settle_terminal(
            run.id, run.user_id, RunStatus.PAUSED, {}, worker_id="worker"
        )


# ---------------------------------------------------------------------------
# 2. 幂等与租约闸门
# ---------------------------------------------------------------------------


async def test_settle_terminal_idempotent_when_terminal_event_exists(
    db_session, user_factory
) -> None:
    """已有终态事件：后到者幂等返回 None——不重复迁移、不重复发事件。"""
    run = await _make_run(db_session, user_factory, start=True, worker="worker")
    stream = AgentEventStream(db_session, AgentEventBroker())

    first = await stream.settle_terminal(
        run.id, run.user_id, RunStatus.COMPLETED, {"outcome": "completed"}, worker_id="worker"
    )
    assert first is not None
    second = await stream.settle_terminal(
        run.id,
        run.user_id,
        RunStatus.FAILED,
        {"outcome": "failed", "error_code": "model_error"},
        worker_id="worker",
    )

    assert second is None
    terminal = await _terminal_events(db_session, run.id)
    assert [row.event_type for row in terminal] == ["run.completed"]
    fresh = await db_session.get(AgentRun, run.id)
    assert fresh.status == RunStatus.COMPLETED


async def test_settle_terminal_completed_requires_active_lease(
    db_session, user_factory
) -> None:
    """completed 只能由持活跃租约的 worker 迁移；租约不符抛 run_lease_not_held。"""
    run = await _make_run(db_session, user_factory, start=True, worker="worker-a")
    stream = AgentEventStream(db_session, AgentEventBroker())

    with pytest.raises(InvalidRunTransition, match="run_lease_not_held"):
        await stream.settle_terminal(
            run.id,
            run.user_id,
            RunStatus.COMPLETED,
            {"outcome": "completed"},
            worker_id="worker-b",
        )

    fresh = await db_session.get(AgentRun, run.id)
    assert fresh.status == RunStatus.RUNNING
    assert await _terminal_events(db_session, run.id) == []


async def test_settle_terminal_failed_skips_when_other_worker_holds_lease(
    db_session, user_factory
) -> None:
    """failed + 租约被他人活跃持有（A4 闸门）：返回 None，不改状态、不发事件。"""
    run = await _make_run(db_session, user_factory, start=True, worker="worker-a")
    stream = AgentEventStream(db_session, AgentEventBroker())

    event = await stream.settle_terminal(
        run.id,
        run.user_id,
        RunStatus.FAILED,
        {"outcome": "failed", "error_code": "executor_error"},
        worker_id="worker-b",
    )

    assert event is None
    fresh = await db_session.get(AgentRun, run.id)
    assert fresh.status == RunStatus.RUNNING
    assert fresh.lease_owner == "worker-a"
    assert await _terminal_events(db_session, run.id) == []


async def test_settle_terminal_heals_terminal_run_missing_event(
    db_session, user_factory
) -> None:
    """旧窗口残留（Run 已终态、无终态事件）：按实际终态补发事件，不再迁移。"""
    run = await _make_run(db_session, user_factory, start=True, worker=None)
    # 直接落 failed 终态但不发事件（模拟 H1 前的崩溃窗口残留）。
    assert await AgentRunRepository(db_session).force_fail(run.id, error_code="legacy")
    stream = AgentEventStream(db_session, AgentEventBroker())

    event = await stream.settle_terminal(
        run.id,
        run.user_id,
        RunStatus.FAILED,
        {"outcome": "failed", "error_code": "executor_error"},
        worker_id="worker-x",
    )

    assert event is not None
    assert event.event_type == "run.failed"
    fresh = await db_session.get(AgentRun, run.id)
    assert fresh.status == RunStatus.FAILED
    terminal = await _terminal_events(db_session, run.id)
    assert [row.event_type for row in terminal] == ["run.failed"]


async def test_settle_terminal_rejects_non_owner(db_session, user_factory) -> None:
    """归属校验与 append 一致：非属主注入终态被拒绝。"""
    run = await _make_run(db_session, user_factory, start=True)
    other = await user_factory()
    stream = AgentEventStream(db_session, AgentEventBroker())

    from app.agent_runtime.events import RunEventForbidden

    with pytest.raises(RunEventForbidden):
        await stream.settle_terminal(
            run.id, other.id, RunStatus.FAILED, {"outcome": "failed"}, worker_id="worker"
        )


# ---------------------------------------------------------------------------
# 3. 并发：恰好一个终态事件
# ---------------------------------------------------------------------------


async def test_concurrent_settle_terminal_emits_exactly_one_event(monkeypatch) -> None:
    """两个独立会话并发收口同一 Run：行锁串行化——恰好一个终态事件，
    后到者幂等返回 None（不是 IntegrityError）。

    门控 ``_terminal_event_locked`` 让 A 在持锁后暂停，强制 B 走到行锁
    等待，确定性覆盖"先查后写"竞态窗口。
    """
    user_id, session_id, run_id = await _create_committed_running_run("worker-a")
    a_holds_lock = asyncio.Event()
    release_a = asyncio.Event()
    original = AgentEventStream._terminal_event_locked

    async def gated(self, rid: str):
        if getattr(self, "_concurrency_gate", None) is not None:
            self._concurrency_gate.set()
            await release_a.wait()
        return await original(self, rid)

    monkeypatch.setattr(AgentEventStream, "_terminal_event_locked", gated)
    try:
        async with SessionFactory() as db_a, SessionFactory() as db_b:
            stream_a = AgentEventStream(db_a, AgentEventBroker())
            stream_a._concurrency_gate = a_holds_lock
            stream_b = AgentEventStream(db_b, AgentEventBroker())

            task_a = asyncio.create_task(
                stream_a.settle_terminal(
                    run_id,
                    user_id,
                    RunStatus.COMPLETED,
                    {"outcome": "completed"},
                    worker_id="worker-a",
                )
            )
            await asyncio.wait_for(a_holds_lock.wait(), timeout=5)
            task_b = asyncio.create_task(
                stream_b.settle_terminal(
                    run_id,
                    user_id,
                    RunStatus.FAILED,
                    {"outcome": "failed", "error_code": "executor_error"},
                    worker_id="worker-b",
                )
            )
            await asyncio.sleep(0.3)
            assert not task_b.done()  # B 被 Run 行锁串行化，等待 A 提交
            release_a.set()
            event_a, event_b = await asyncio.gather(task_a, task_b)

        assert event_a is not None and event_a.event_type == "run.completed"
        assert event_b is None  # 后到者幂等，不是唯一键异常

        async with SessionFactory() as db:
            terminal = [
                row
                for row in (
                    await db.scalars(select(AgentEvent).where(AgentEvent.run_id == run_id))
                ).all()
                if is_terminal_event(row.event_type)
            ]
            assert [row.event_type for row in terminal] == ["run.completed"]
            run = await db.get(AgentRun, run_id)
            assert run.status == RunStatus.COMPLETED
    finally:
        await _purge_committed(user_id, session_id, run_id)


async def test_concurrent_append_terminal_once_emits_exactly_one_event(monkeypatch) -> None:
    """append_terminal_once 的"先查后写"竞态（Gate A P1 直击）：两个独立会话
    并发补发终态事件 → 恰好一个事件、另一方幂等 None，绝不抛 IntegrityError。
    """
    user_id, session_id, run_id = await _create_committed_run()
    a_holds_lock = asyncio.Event()
    release_a = asyncio.Event()
    original = AgentEventStream._terminal_event_locked

    async def gated(self, rid: str):
        if getattr(self, "_concurrency_gate", None) is not None:
            self._concurrency_gate.set()
            await release_a.wait()
        return await original(self, rid)

    monkeypatch.setattr(AgentEventStream, "_terminal_event_locked", gated)
    try:
        async with SessionFactory() as db_a, SessionFactory() as db_b:
            stream_a = AgentEventStream(db_a, AgentEventBroker())
            stream_a._concurrency_gate = a_holds_lock
            stream_b = AgentEventStream(db_b, AgentEventBroker())

            task_a = asyncio.create_task(
                stream_a.append_terminal_once(run_id, user_id, "run.completed", {})
            )
            await asyncio.wait_for(a_holds_lock.wait(), timeout=5)
            task_b = asyncio.create_task(
                stream_b.append_terminal_once(run_id, user_id, "run.failed", {})
            )
            await asyncio.sleep(0.3)
            assert not task_b.done()
            release_a.set()
            event_a, event_b = await asyncio.gather(task_a, task_b)

        assert event_a is not None and event_a.event_type == "run.completed"
        assert event_b is None

        async with SessionFactory() as db:
            terminal = [
                row
                for row in (
                    await db.scalars(select(AgentEvent).where(AgentEvent.run_id == run_id))
                ).all()
                if is_terminal_event(row.event_type)
            ]
            assert [row.event_type for row in terminal] == ["run.completed"]
            sequences = [
                row.sequence
                for row in (
                    await db.scalars(select(AgentEvent).where(AgentEvent.run_id == run_id))
                ).all()
            ]
            assert sequences == [1]  # 无重复 sequence（无唯一键冲突）
    finally:
        await _purge_committed(user_id, session_id, run_id)


# ---------------------------------------------------------------------------
# 4. 崩溃注入：事务中途异常 → 整体回滚 → 恢复后再次收口成功
# ---------------------------------------------------------------------------


async def test_settle_terminal_crash_rolls_back_migration_and_event(monkeypatch) -> None:
    """终态事务在"Run 状态已迁移、终态事件未写"的窗口注入崩溃（事件插入
    抛错）：整体回滚——Run 保持非终态、零事件；恢复后再次收口成功发事件。
    """
    user_id, session_id, run_id = await _create_committed_running_run("worker-a")

    async def boom(run, event_type, payload):
        raise RuntimeError("injected terminal-transaction crash")

    try:
        async with SessionFactory() as db_a:
            stream_a = AgentEventStream(db_a, AgentEventBroker())
            monkeypatch.setattr(stream_a, "_insert_terminal_locked", boom)
            with pytest.raises(RuntimeError, match="injected terminal-transaction crash"):
                await stream_a.settle_terminal(
                    run_id,
                    user_id,
                    RunStatus.COMPLETED,
                    {"outcome": "completed"},
                    worker_id="worker-a",
                )
            await db_a.rollback()

        async with SessionFactory() as db_b:
            # 整体回滚：迁移不落库——Run 保持 running、无 completed_at、零事件。
            run = await db_b.get(AgentRun, run_id)
            assert run.status == RunStatus.RUNNING
            assert run.completed_at is None
            event_count = await db_b.scalar(
                select(func.count(AgentEvent.id)).where(AgentEvent.run_id == run_id)
            )
            assert event_count == 0

            # 恢复（接管方/重试）再次收口：成功迁移 + 发出终态事件。
            recovered = await AgentEventStream(db_b, AgentEventBroker()).settle_terminal(
                run_id,
                user_id,
                RunStatus.FAILED,
                {"outcome": "failed", "error_code": "executor_error"},
                worker_id="worker-a",
            )
            assert recovered is not None
            assert recovered.event_type == "run.failed"

        async with SessionFactory() as db_c:
            run = await db_c.get(AgentRun, run_id)
            assert run.status == RunStatus.FAILED
            terminal = [
                row
                for row in (
                    await db_c.scalars(select(AgentEvent).where(AgentEvent.run_id == run_id))
                ).all()
                if is_terminal_event(row.event_type)
            ]
            assert [row.event_type for row in terminal] == ["run.failed"]
    finally:
        await _purge_committed(user_id, session_id, run_id)
