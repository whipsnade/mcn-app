from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.agent_runtime.models import AgentRun, AgentRunAttempt, AgentSession
from app.agent_runtime.repository import (
    ATTEMPT_MAX_DECISIONS,
    ATTEMPT_MAX_SECONDS,
    AgentRunRepository,
)
from app.agent_runtime.state import InvalidRunTransition, RunStatus


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _create_queued_run(db_session, user_factory) -> tuple[str, str]:
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


def test_attempt_guardrail_constants() -> None:
    assert ATTEMPT_MAX_DECISIONS == 50
    assert ATTEMPT_MAX_SECONDS == 30 * 60


async def test_begin_attempt_creates_first_attempt_and_moves_queued_to_running(
    db_session, user_factory
) -> None:
    user_id, run_id = await _create_queued_run(db_session, user_factory)
    repo = AgentRunRepository(db_session)

    attempt = await repo.begin_attempt(run_id)

    assert attempt.attempt == 1
    assert attempt.decision_count == 0
    assert attempt.outcome == "running"
    run = await db_session.get(AgentRun, run_id)
    assert run.status == "running"
    assert run.started_at is not None


async def test_begin_attempt_resumed_requires_paused_and_resets_attempt_counters(
    db_session, user_factory
) -> None:
    user_id, run_id = await _create_queued_run(db_session, user_factory)
    repo = AgentRunRepository(db_session)
    await repo.begin_attempt(run_id)
    await repo.claim_lease(run_id, "worker-a", 300)
    run = await db_session.get(AgentRun, run_id)
    run.decision_count = 7
    await db_session.flush()
    await repo.pause(run_id, "worker-a")

    attempt2 = await repo.begin_attempt(run_id, resumed=True)

    assert attempt2.attempt == 2
    assert attempt2.decision_count == 0
    run = await db_session.get(AgentRun, run_id)
    assert run.status == "running"
    # run.decision_count 是跨 Attempt 的累计审计值，恢复时不重置
    assert run.decision_count == 7


async def test_begin_attempt_resumed_on_non_paused_run_raises(
    db_session, user_factory
) -> None:
    user_id, run_id = await _create_queued_run(db_session, user_factory)
    repo = AgentRunRepository(db_session)
    await repo.begin_attempt(run_id)

    with pytest.raises(InvalidRunTransition) as exc_info:
        await repo.begin_attempt(run_id, resumed=True)

    assert str(exc_info.value) == "run_not_paused"


async def test_claim_lease_succeeds_on_running_run_without_conflict(
    db_session, user_factory
) -> None:
    user_id, run_id = await _create_queued_run(db_session, user_factory)
    repo = AgentRunRepository(db_session)
    await repo.begin_attempt(run_id)
    # MySQL DATETIME 精度为秒，注入无微秒的时钟保证精确断言
    now = utc_now().replace(microsecond=0)

    claimed = await repo.claim_lease(run_id, "worker-a", 300, now=now)

    assert claimed is True
    run = await db_session.get(AgentRun, run_id)
    assert run.lease_owner == "worker-a"
    assert run.lease_expires_at == now + timedelta(seconds=300)
    assert run.heartbeat_at == now


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
async def test_claim_lease_refuses_terminal_runs(
    db_session, user_factory, status
) -> None:
    user_id, run_id = await _create_queued_run(db_session, user_factory)
    repo = AgentRunRepository(db_session)
    await repo.begin_attempt(run_id)
    run = await db_session.get(AgentRun, run_id)
    run.status = status
    await db_session.flush()

    assert await repo.claim_lease(run_id, "worker-a", 300) is False
    run = await db_session.get(AgentRun, run_id)
    assert run.lease_owner is None


async def test_claim_lease_refuses_clarification_requested_run(
    db_session, user_factory
) -> None:
    user_id, run_id = await _create_queued_run(db_session, user_factory)
    repo = AgentRunRepository(db_session)
    await repo.begin_attempt(run_id)
    run = await db_session.get(AgentRun, run_id)
    run.status = "clarification_requested"
    await db_session.flush()

    assert await repo.claim_lease(run_id, "worker-a", 300) is False


async def test_claim_lease_refuses_unexpired_lease_held_by_other_worker(
    db_session, user_factory
) -> None:
    user_id, run_id = await _create_queued_run(db_session, user_factory)
    repo = AgentRunRepository(db_session)
    await repo.begin_attempt(run_id)
    run = await db_session.get(AgentRun, run_id)
    run.lease_owner = "worker-a"
    run.lease_expires_at = utc_now() + timedelta(seconds=300)
    await db_session.flush()

    assert await repo.claim_lease(run_id, "worker-b", 300) is False


async def test_claim_lease_reclaims_expired_lease(db_session, user_factory) -> None:
    user_id, run_id = await _create_queued_run(db_session, user_factory)
    repo = AgentRunRepository(db_session)
    await repo.begin_attempt(run_id)
    run = await db_session.get(AgentRun, run_id)
    run.lease_owner = "worker-a"
    run.lease_expires_at = utc_now() - timedelta(seconds=10)
    await db_session.flush()

    assert await repo.claim_lease(run_id, "worker-b", 300) is True
    run = await db_session.get(AgentRun, run_id)
    assert run.lease_owner == "worker-b"
    assert run.lease_expires_at is not None


async def test_pause_releases_lease_and_pauses_run(db_session, user_factory) -> None:
    user_id, run_id = await _create_queued_run(db_session, user_factory)
    repo = AgentRunRepository(db_session)
    await repo.begin_attempt(run_id)
    await repo.claim_lease(run_id, "worker-a", 300)

    assert await repo.pause(run_id, "worker-a") is True
    run = await db_session.get(AgentRun, run_id)
    assert run.status == "paused"
    assert run.lease_owner is None
    assert run.lease_expires_at is None
    assert run.paused_at is not None

    attempts = list(
        (
            await db_session.scalars(
                select(AgentRunAttempt).where(AgentRunAttempt.run_id == run_id)
            )
        ).all()
    )
    assert len(attempts) == 1
    assert attempts[0].outcome == "paused"
    assert attempts[0].ended_at is not None


async def test_pause_by_non_owner_is_rejected(db_session, user_factory) -> None:
    user_id, run_id = await _create_queued_run(db_session, user_factory)
    repo = AgentRunRepository(db_session)
    await repo.begin_attempt(run_id)
    await repo.claim_lease(run_id, "worker-a", 300)

    assert await repo.pause(run_id, "worker-b") is False
    run = await db_session.get(AgentRun, run_id)
    assert run.status == "running"
    assert run.lease_owner == "worker-a"


async def test_cancel_transitions_running_to_cancelled_and_blocks_further_transitions(
    db_session, user_factory
) -> None:
    user_id, run_id = await _create_queued_run(db_session, user_factory)
    repo = AgentRunRepository(db_session)
    await repo.begin_attempt(run_id)
    await repo.claim_lease(run_id, "worker-a", 300)

    assert await repo.cancel(run_id, user_id) is True
    run = await db_session.get(AgentRun, run_id)
    assert run.status == "cancelled"
    assert run.lease_owner is None
    assert run.completed_at is not None

    # cancelled 拒绝再次取消与任何状态迁移
    assert await repo.cancel(run_id, user_id) is False
    with pytest.raises(InvalidRunTransition):
        await repo.transition(run_id, RunStatus.RUNNING, worker_id="worker-a")


async def test_claim_lease_refuses_run_with_cancel_requested(
    db_session, user_factory
) -> None:
    user_id, run_id = await _create_queued_run(db_session, user_factory)
    repo = AgentRunRepository(db_session)
    await repo.begin_attempt(run_id)
    run = await db_session.get(AgentRun, run_id)
    run.cancel_requested = True
    await db_session.flush()

    assert await repo.claim_lease(run_id, "worker-a", 300) is False
    run = await db_session.get(AgentRun, run_id)
    assert run.lease_owner is None


async def test_request_cancel_sets_signal_and_blocks_claim(
    db_session, user_factory
) -> None:
    user_id, run_id = await _create_queued_run(db_session, user_factory)
    repo = AgentRunRepository(db_session)
    await repo.begin_attempt(run_id)
    await repo.claim_lease(run_id, "worker-a", 300)

    assert await repo.request_cancel(run_id, user_id) is True
    run = await db_session.get(AgentRun, run_id)
    assert run.cancel_requested is True
    # 信号持久化后，任何 worker 都不能再抢占该 Run
    assert await repo.claim_lease(run_id, "worker-b", 300) is False


async def test_request_cancel_on_terminal_run_returns_false(
    db_session, user_factory
) -> None:
    user_id, run_id = await _create_queued_run(db_session, user_factory)
    repo = AgentRunRepository(db_session)
    await repo.begin_attempt(run_id)
    await repo.cancel(run_id, user_id)

    assert await repo.request_cancel(run_id, user_id) is False


async def test_request_cancel_is_idempotent(db_session, user_factory) -> None:
    user_id, run_id = await _create_queued_run(db_session, user_factory)
    repo = AgentRunRepository(db_session)
    await repo.begin_attempt(run_id)

    assert await repo.request_cancel(run_id, user_id) is True
    assert await repo.request_cancel(run_id, user_id) is True
    run = await db_session.get(AgentRun, run_id)
    assert run.cancel_requested is True


async def test_transition_to_clarification_requested_closes_attempt(
    db_session, user_factory
) -> None:
    user_id, run_id = await _create_queued_run(db_session, user_factory)
    repo = AgentRunRepository(db_session)
    await repo.begin_attempt(run_id)
    await repo.claim_lease(run_id, "worker-a", 300)

    run = await repo.transition(
        run_id, RunStatus.CLARIFICATION_REQUESTED, worker_id="worker-a"
    )

    assert run.status == "clarification_requested"
    attempts = list(
        (
            await db_session.scalars(
                select(AgentRunAttempt).where(AgentRunAttempt.run_id == run_id)
            )
        ).all()
    )
    assert len(attempts) == 1
    assert attempts[0].outcome == "completed"
    assert attempts[0].ended_at is not None


async def test_transition_reviewing_to_completed_is_terminal_and_releases_lease(
    db_session, user_factory
) -> None:
    user_id, run_id = await _create_queued_run(db_session, user_factory)
    repo = AgentRunRepository(db_session)
    await repo.begin_attempt(run_id)
    await repo.claim_lease(run_id, "worker-a", 300)

    run = await repo.transition(run_id, RunStatus.REVIEWING, worker_id="worker-a")
    assert run.status == "reviewing"

    run = await repo.transition(run_id, RunStatus.COMPLETED, worker_id="worker-a")
    assert run.status == "completed"
    assert run.completed_at is not None
    assert run.lease_owner is None
    assert run.lease_expires_at is None


async def test_resume_after_pause_resets_attempt_counters_on_every_new_attempt(
    db_session, user_factory
) -> None:
    user_id, run_id = await _create_queued_run(db_session, user_factory)
    repo = AgentRunRepository(db_session)
    await repo.begin_attempt(run_id)
    await repo.claim_lease(run_id, "worker-a", 300)
    run = await db_session.get(AgentRun, run_id)
    run.decision_count = 7
    await db_session.flush()
    await repo.pause(run_id, "worker-a")

    attempt2 = await repo.begin_attempt(run_id, resumed=True)
    assert attempt2.attempt == 2
    assert attempt2.decision_count == 0
    run = await db_session.get(AgentRun, run_id)
    assert run.decision_count == 7

    await repo.claim_lease(run_id, "worker-b", 300)
    await repo.pause(run_id, "worker-b")
    run = await db_session.get(AgentRun, run_id)
    run.decision_count = 9
    await db_session.flush()

    attempt3 = await repo.begin_attempt(run_id, resumed=True)
    assert attempt3.attempt == 3
    assert attempt3.decision_count == 0
    run = await db_session.get(AgentRun, run_id)
    assert run.status == "running"
    assert run.decision_count == 9


async def test_injectable_clock_controls_timestamps(
    db_session, user_factory, monkeypatch
) -> None:
    fixed = utc_now().replace(microsecond=0)
    monkeypatch.setattr("app.agent_runtime.repository.utc_now", lambda: fixed)

    user_id, run_id = await _create_queued_run(db_session, user_factory)
    repo = AgentRunRepository(db_session)
    await repo.begin_attempt(run_id)
    await repo.claim_lease(run_id, "worker-a", 300)
    await repo.pause(run_id, "worker-a")

    run = await db_session.get(AgentRun, run_id)
    assert run.started_at == fixed
    assert run.paused_at == fixed
    assert run.lease_owner is None
