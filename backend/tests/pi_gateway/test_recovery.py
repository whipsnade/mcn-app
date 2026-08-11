from datetime import timedelta
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select

from app.agent_runtime.events import AgentEvent, AgentEventBroker
from app.agent_runtime.models import AgentMessage, AgentRunAttempt, AgentStep, AgentToolCall
from app.agent_runtime.recovery import RecoveryLoop
from app.agent_runtime.repository import utc_now
from app.agent_runtime.state import RunStatus
from app.agent_runtime.tools.mcp import DEFINITELY_NOT_SENT, AgentMcpAccounting
from app.pi_gateway.service import PiGatewayRecoveryService, hash_lease_token
from tests.agent_runtime.test_recovery import _funded_user, _make_chain


class _FakeExecutor:
    def __init__(self) -> None:
        self.processed: list[str] = []
        self.cancelled: list[str] = []

    async def process_run(self, run_id: str, *, worker_id: str) -> None:
        self.processed.append(run_id)

    async def settle_cancel_requested(self, run_id: str) -> bool:
        self.cancelled.append(run_id)
        return True


@asynccontextmanager
async def _session(db_session):
    yield db_session


@pytest.mark.asyncio
async def test_pi_recovery_closes_idempotently_on_durable_completion(db_session, user_factory) -> None:
    """terminal ACK 丢失（completion 已持久化但 gateway 失联）：恢复必须按
    durable completion 幂等收口 completed——不新起 Attempt（新 Attempt 会
    重放模型与 MCP 外发）、不消耗基础设施重试。"""
    user = await _funded_user(db_session, user_factory)
    session, run, _step = await _make_chain(db_session, user.id)
    run.runtime_backend = "pi"
    session.active_run_id = run.id
    now = utc_now()
    run.gateway_id = "gateway-ack-lost"
    run.gateway_lease_hash = hash_lease_token("lease-token-ack-lost")
    run.gateway_lease_expires_at = now - timedelta(seconds=1)
    run.lease_owner = run.gateway_id
    run.lease_expires_at = run.gateway_lease_expires_at
    db_session.add(
        AgentMessage(
            id="msg-ack-lost-completion",
            session_id=session.id,
            run_id=run.id,
            role="assistant",
            content="最终结论",
            metadata_json={"gateway_message": True},
            sequence=2,
            created_at=now,
        )
    )
    await db_session.flush()

    recovery = PiGatewayRecoveryService(db_session, broker=AgentEventBroker(), now_fn=lambda: now)

    assert await recovery.recover_expired_run(run.id) == "completed"
    assert run.status == RunStatus.COMPLETED
    assert run.infrastructure_retry_count == 0
    attempts = list(
        (await db_session.scalars(select(AgentRunAttempt).where(AgentRunAttempt.run_id == run.id))).all()
    )
    assert len(attempts) == 1
    assert attempts[0].outcome != "running"
    assert session.active_run_id is None
    terminal_events = [
        event.event_type
        for event in (await db_session.scalars(select(AgentEvent).where(AgentEvent.run_id == run.id))).all()
        if event.event_type.startswith("run.")
    ]
    assert terminal_events == ["run.completed"]


@pytest.mark.asyncio
async def test_pi_recovery_loop_does_not_reclaim_current_runs(db_session, user_factory) -> None:
    user = await _funded_user(db_session, user_factory)
    _current_session, current_run, _ = await _make_chain(db_session, user.id)
    current_run.runtime_backend = "current"
    _pi_session, pi_run, _ = await _make_chain(db_session, user.id)
    pi_run.runtime_backend = "pi"
    expired = utc_now() - timedelta(seconds=10)
    current_run.lease_expires_at = expired
    pi_run.lease_expires_at = expired
    await db_session.flush()

    executor = _FakeExecutor()
    recovery = RecoveryLoop(
        executor=executor,
        session_factory=lambda: _session(db_session),
        tool_factory=lambda _db, _call: None,
        worker_id="pi-recovery",
        runtime_backend="pi",
        pi_recovery=lambda _run_id: _requeue_marker(),
    )

    reclaimed = await recovery.reclaim_expired_runs()

    assert current_run.id not in reclaimed
    assert pi_run.id in reclaimed
    assert executor.processed == []


async def _requeue_marker() -> str:
    return "requeued"


@pytest.mark.asyncio
async def test_pi_recovery_requeues_once_then_fails_closed(db_session, user_factory) -> None:
    user = await _funded_user(db_session, user_factory)
    session, run, _step = await _make_chain(db_session, user.id)
    run.runtime_backend = "pi"
    session.active_run_id = run.id
    now = utc_now()
    run.gateway_id = "gateway-dead"
    run.gateway_lease_hash = hash_lease_token("lease-token-with-enough-entropy")
    run.gateway_lease_expires_at = now - timedelta(seconds=1)
    run.lease_owner = run.gateway_id
    run.lease_expires_at = run.gateway_lease_expires_at
    await db_session.flush()

    recovery = PiGatewayRecoveryService(
        db_session,
        broker=AgentEventBroker(),
        now_fn=lambda: now,
    )
    assert await recovery.recover_expired_run(run.id) == "requeued"
    assert run.status == RunStatus.QUEUED
    assert run.infrastructure_retry_count == 1
    assert run.gateway_lease_hash is None
    first_attempt = await db_session.scalar(
        select(AgentRunAttempt).where(AgentRunAttempt.run_id == run.id)
    )
    assert first_attempt is not None and first_attempt.outcome == "failed"

    second_attempt = AgentRunAttempt(
        id="attempt-pi-recovery-2",
        run_id=run.id,
        attempt=2,
        started_at=now,
        outcome="running",
        decision_count=0,
    )
    db_session.add(second_attempt)
    # 第二次恢复时仍有开放 ToolCall：必须在同一事务内置 unknown 并关闭 Attempt 2。
    open_call = AgentToolCall(
        id="tool-call-second-attempt-open",
        run_id=run.id,
        step_id=_step.id,
        logical_call_id="logical-second-attempt-open",
        service="insight_cube",
        internal_tool_name="query_analysis_data",
        arguments_hash="f" * 64,
        status="running",
        points_reserved=10,
        started_at=now,
    )
    step2 = AgentStep(
        id="step-pi-recovery-2",
        run_id=run.id,
        attempt_id=second_attempt.id,
        sequence=2,
        step_type="tool_call",
        status="running",
        visibility="user",
        created_at=now,
    )
    db_session.add(step2)
    await db_session.flush()
    open_call.step_id = step2.id
    db_session.add(open_call)
    run.status = RunStatus.RUNNING
    run.gateway_id = "gateway-dead"
    run.gateway_lease_hash = hash_lease_token("lease-token-second-attempt")
    run.gateway_lease_expires_at = now - timedelta(seconds=1)
    run.lease_owner = run.gateway_id
    run.lease_expires_at = run.gateway_lease_expires_at
    await db_session.flush()

    assert await recovery.recover_expired_run(run.id) == "failed"
    assert run.status == RunStatus.FAILED
    assert run.infrastructure_retry_count == 1
    # 第二次恢复必须同事务关闭 Attempt 2、处理开放 ToolCall、释放 lease/session
    await db_session.refresh(second_attempt)
    assert second_attempt.outcome == "failed"
    assert second_attempt.ended_at is not None
    await db_session.refresh(open_call)
    assert open_call.status == "unknown"
    assert session.active_run_id is None
    assert run.gateway_lease_hash is None
    events = list(
        (
            await db_session.scalars(
                select(AgentEvent).where(AgentEvent.run_id == run.id)
            )
        ).all()
    )
    assert [event.event_type for event in events if event.event_type.startswith("run.")] == [
        "run.failed"
    ]


@pytest.mark.asyncio
async def test_pi_recovery_marks_open_tool_calls_unknown_before_requeue(
    db_session, user_factory
) -> None:
    user = await _funded_user(db_session, user_factory)
    _session, run, step = await _make_chain(db_session, user.id)
    run.runtime_backend = "pi"
    now = utc_now()
    run.gateway_id = "gateway-dead"
    run.gateway_lease_hash = hash_lease_token("lease-token-open-calls")
    run.gateway_lease_expires_at = now - timedelta(seconds=1)
    run.lease_owner = run.gateway_id
    run.lease_expires_at = run.gateway_lease_expires_at
    running = AgentToolCall(
        id="tool-call-running",
        run_id=run.id,
        step_id=step.id,
        logical_call_id="logical-running",
        service="insight_cube",
        internal_tool_name="query_analysis_data",
        arguments_hash="a" * 64,
        status="running",
        points_reserved=10,
        started_at=now,
    )
    reserved = AgentToolCall(
        id="tool-call-reserved",
        run_id=run.id,
        step_id=step.id,
        logical_call_id="logical-reserved",
        service="insight_cube",
        internal_tool_name="query_analysis_data",
        arguments_hash="b" * 64,
        status="reserved",
        points_reserved=10,
        started_at=now,
    )
    settled = AgentToolCall(
        id="tool-call-settled",
        run_id=run.id,
        step_id=step.id,
        logical_call_id="logical-settled",
        service="insight_cube",
        internal_tool_name="query_analysis_data",
        arguments_hash="c" * 64,
        status="settled",
        points_reserved=10,
        started_at=now,
    )
    db_session.add_all([running, reserved, settled])
    await db_session.flush()

    recovery = PiGatewayRecoveryService(db_session, now_fn=lambda: now)

    assert await recovery.recover_expired_run(run.id) == "requeued"
    assert running.status == "unknown"
    assert reserved.status == "unknown"
    assert settled.status == "settled"
    assert running.points_reserved == 10
    assert reserved.points_reserved == 10


@pytest.mark.asyncio
async def test_pi_recovery_cancels_expired_gateway_run_without_new_attempt(db_session, user_factory) -> None:
    user = await _funded_user(db_session, user_factory)
    session, run, step = await _make_chain(db_session, user.id)
    run.runtime_backend = "pi"
    run.cancel_requested = True
    session.active_run_id = run.id
    now = utc_now()
    run.gateway_id = "gateway-cancelled"
    run.gateway_lease_hash = hash_lease_token("lease-token-cancelled")
    run.gateway_lease_expires_at = now - timedelta(seconds=1)
    run.lease_owner = run.gateway_id
    run.lease_expires_at = run.gateway_lease_expires_at
    running_call = AgentToolCall(
        id="tool-call-cancelled-running",
        run_id=run.id,
        step_id=step.id,
        logical_call_id="logical-cancelled-running",
        service="insight_cube",
        internal_tool_name="query_analysis_data",
        arguments_hash="d" * 64,
        status="planned",
        started_at=now,
    )
    reserved_call = AgentToolCall(
        id="tool-call-cancelled-reserved",
        run_id=run.id,
        step_id=step.id,
        logical_call_id="logical-cancelled-reserved",
        service="insight_cube",
        internal_tool_name="query_analysis_data",
        arguments_hash="e" * 64,
        status="planned",
        started_at=now,
    )
    db_session.add_all([running_call, reserved_call])
    await db_session.flush()
    accounting = AgentMcpAccounting(db_session)
    await accounting.reserve(user.id, running_call)
    await accounting.mark_running(running_call)
    await accounting.reserve(user.id, reserved_call)

    recovery = PiGatewayRecoveryService(
        db_session,
        broker=AgentEventBroker(),
        now_fn=lambda: now,
    )

    assert await recovery.cancel_expired_run(run.id) is True
    assert run.status == RunStatus.CANCELLED
    assert run.infrastructure_retry_count == 0
    assert session.active_run_id is None
    assert running_call.status == "unknown"
    assert running_call.points_reserved == 10
    assert reserved_call.status == "failed"
    assert reserved_call.error_type == DEFINITELY_NOT_SENT
    assert reserved_call.points_reserved == 0
    attempt_run_id = await db_session.scalar(
        select(AgentRunAttempt.run_id).where(AgentRunAttempt.run_id == run.id)
    )
    assert attempt_run_id == run.id
