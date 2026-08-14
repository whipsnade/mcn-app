"""RunLeaseHeartbeat 单元测试（v3 加固 §5.5 / A4）。

心跳用独立 DB Session 每 ``lease_seconds / 3`` 续租一次；发现租约已被其他
worker 接管（renew 明确失败）时置 lost——旧 worker 必须停止且不得再发布
Artifact 或写 Run 终态。瞬时异常（DB 抖动）不判死，下轮重试。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4

from app.agent_runtime.heartbeat import RunLeaseHeartbeat
from app.agent_runtime.models import AgentRun, AgentSession
from app.agent_runtime.repository import AgentRunRepository, utc_now
from app.agent_runtime.state import RunStatus


@asynccontextmanager
async def _shared_session(db_session):
    yield db_session


async def _make_running_run(db_session, user_factory, *, worker: str, lease_seconds: int):
    user = await user_factory()
    now = utc_now()
    session = AgentSession(
        id=str(uuid4()),
        user_id=user.id,
        title="心跳测试会话",
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
    repo = AgentRunRepository(db_session)
    await repo.begin_attempt(run.id)
    await repo.claim_lease(run.id, worker, lease_seconds)
    return run


async def test_heartbeat_renews_lease_until_stopped(db_session, user_factory) -> None:
    """持有期间持续续租：lease_expires_at 随心跳不断后移，lost 保持 False。"""
    run = await _make_running_run(db_session, user_factory, worker="worker-a", lease_seconds=1)
    original_expires = (await db_session.get(AgentRun, run.id)).lease_expires_at
    heartbeat = RunLeaseHeartbeat(
        session_factory=lambda: _shared_session(db_session),
        run_id=run.id,
        worker_id="worker-a",
        lease_seconds=1,
        interval_seconds=0.05,
    )
    await heartbeat.start()
    try:
        await asyncio.sleep(0.25)
        fresh = await db_session.get(AgentRun, run.id)
        assert fresh.lease_expires_at > original_expires
        assert fresh.lease_owner == "worker-a"
        assert not heartbeat.lost
    finally:
        await heartbeat.stop()


async def test_heartbeat_marks_lost_after_takeover(db_session, user_factory) -> None:
    """租约被其他 worker 接管后：renew 明确失败 → lost（不再继续续租）。"""
    run = await _make_running_run(db_session, user_factory, worker="worker-a", lease_seconds=1)
    heartbeat = RunLeaseHeartbeat(
        session_factory=lambda: _shared_session(db_session),
        run_id=run.id,
        worker_id="worker-a",
        lease_seconds=1,
        interval_seconds=0.05,
    )
    await heartbeat.start()
    try:
        # 模拟另一 worker 接管租约
        row = await db_session.get(AgentRun, run.id)
        row.lease_owner = "worker-b"
        row.lease_expires_at = utc_now() + timedelta(seconds=300)
        await db_session.flush()
        for _ in range(40):
            if heartbeat.lost:
                break
            await asyncio.sleep(0.05)
        assert heartbeat.lost
        # lost 后不再续租：worker-b 的租约不被旧 worker 触碰
        fresh = await db_session.get(AgentRun, run.id)
        assert fresh.lease_owner == "worker-b"
    finally:
        await heartbeat.stop()


async def test_heartbeat_tolerates_transient_session_failure(
    db_session, user_factory
) -> None:
    """会话工厂瞬时异常不判死：记日志后下轮重试，lost 保持 False。"""
    run = await _make_running_run(db_session, user_factory, worker="worker-a", lease_seconds=1)
    calls = {"count": 0}

    @asynccontextmanager
    async def flaky_factory():
        calls["count"] += 1
        if calls["count"] <= 2:
            raise RuntimeError("transient db error")
        yield db_session

    heartbeat = RunLeaseHeartbeat(
        session_factory=flaky_factory,
        run_id=run.id,
        worker_id="worker-a",
        lease_seconds=1,
        interval_seconds=0.05,
    )
    await heartbeat.start()
    try:
        await asyncio.sleep(0.3)
        assert not heartbeat.lost
        assert calls["count"] > 2  # 前两次失败后仍在重试
    finally:
        await heartbeat.stop()


async def test_heartbeat_stop_is_idempotent(db_session, user_factory) -> None:
    """stop 幂等：未 start 或重复 stop 都安全；start 幂等不重复建任务。"""
    run = await _make_running_run(db_session, user_factory, worker="worker-a", lease_seconds=1)
    heartbeat = RunLeaseHeartbeat(
        session_factory=lambda: _shared_session(db_session),
        run_id=run.id,
        worker_id="worker-a",
        lease_seconds=1,
        interval_seconds=0.05,
    )
    await heartbeat.stop()  # 未 start 也安全
    await heartbeat.start()
    await heartbeat.start()  # 幂等
    await heartbeat.stop()
    await heartbeat.stop()
    assert not heartbeat.lost
    assert RunStatus((await db_session.get(AgentRun, run.id)).status) == RunStatus.RUNNING
