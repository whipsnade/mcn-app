"""RecoveryLoop 集成测试（Task 15 / 设计文档 §11.1 / §11.2）。

覆盖：
1. 过期租约 Run 被恢复循环领取重建 Attempt，引擎继续执行到终态；
2. unknown MCP 调用只经 AgentMcpTool.reconcile 按 logical_call_id 只读核对，
   绝不重发原工具；四种结果（确认成功带 payload / 确认成功无 payload /
   确认失败 / 无法核对）分别 settle / release / 保持预留；
3. 无法核对的 unknown 保持 reserved + keep_unknown 审计，并产生运维告警；
   不使用旧的 release_expired_unknown 超时自动释放策略。
"""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, Mapping
from uuid import uuid4

from sqlalchemy import select

from app.agent_runtime.engine import AgentEngine
from app.agent_runtime.events import AgentEventBroker, AgentEventStream
from app.agent_runtime.executor import AgentRunExecutor
from app.agent_runtime.models import (
    AgentRun,
    AgentRunAttempt,
    AgentSession,
    AgentStep,
    AgentToolCall,
    AgentToolCallReconciliation,
    EvidenceItem,
)
from app.agent_runtime.recovery import RecoveryLoop
from app.agent_runtime.repository import utc_now
from app.agent_runtime.reviewer import ReviewerDriver
from app.agent_runtime.schemas import Complete
from app.agent_runtime.state import RunStatus
from app.agent_runtime.tools.mcp import MCP_POINTS_COST, AgentMcpTool, logical_call_id_for
from app.agent_runtime.tools.registry import ToolRegistry
from app.billing.service import WalletService
from app.mcp_gateway.contracts import DataTapService
from app.mcp_gateway.transport import RemoteToolResult
from app.mcp_gateway.validation import canonical_json_bytes

INPUT_SCHEMA = {
    "type": "object",
    "properties": {"keyword": {"type": "string"}},
    "required": ["keyword"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"result": {"type": "string"}},
    "required": ["result"],
    "additionalProperties": False,
}
INTERNAL_NAME = "query_analysis_data"
REMOTE_NAME = "datatap.insight.query.analysis.v1"


class FakeAgentGateway:
    def __init__(self, actions: list[Any]) -> None:
        self.actions = list(actions)
        self.calls: list[dict[str, Any]] = []

    async def decide(self, *, run, attempt_id, profile, messages, thinking_sink=None, **kwargs) -> Any:
        self.calls.append({"run_id": run.id})
        if not self.actions:
            raise AssertionError("fake agent gateway exhausted")
        return self.actions.pop(0)


class _FakeReviewerGateway:
    async def decide(self, **kwargs: Any) -> Any:
        raise AssertionError("reviewer gateway should not be called")


class FakeMcpTransport:
    """记录调用并按序吐出预编排结果；reconcile_tool_call 只读核对、绝不重发。"""

    def __init__(self) -> None:
        self.calls: list[tuple[DataTapService, str, dict[str, Any]]] = []
        self.reconciled: dict[str, RemoteToolResult] = {}

    async def call_tool(self, service: DataTapService, remote_name: str, arguments: Mapping[str, Any]):
        self.calls.append((service, remote_name, dict(arguments)))
        raise AssertionError("recovery must never re-send the original tool")

    async def reconcile_tool_call(self, upstream_request_id: str) -> RemoteToolResult | None:
        return self.reconciled.get(upstream_request_id)


@asynccontextmanager
async def _shared_session(db_session):
    yield db_session


async def _make_chain(db_session, user_id: str):
    now = utc_now()
    session = AgentSession(
        id=str(uuid4()), user_id=user_id, title="恢复测试会话", status="active",
        created_at=now, updated_at=now,
    )
    db_session.add(session)
    await db_session.flush()
    run = AgentRun(
        id=str(uuid4()), session_id=session.id, user_id=user_id,
        run_kind="user", visibility="user",
        profile_name="session_analyst_v1", profile_version="v1", model="test-model",
        status="running", decision_count=0, review_count=0, revision_count=0,
        started_at=now,
    )
    db_session.add(run)
    await db_session.flush()
    attempt = AgentRunAttempt(
        id=str(uuid4()), run_id=run.id, attempt=1, started_at=now,
        decision_count=0, outcome="running",
    )
    db_session.add(attempt)
    await db_session.flush()
    step = AgentStep(
        id=str(uuid4()), run_id=run.id, attempt_id=attempt.id, sequence=1,
        step_type="tool_call", status="running", visibility="user", created_at=now,
    )
    db_session.add(step)
    await db_session.flush()
    return session, run, step


def _bridge(db_session, transport: FakeMcpTransport) -> AgentMcpTool:
    return AgentMcpTool(
        internal_name=INTERNAL_NAME,
        service=DataTapService.INSIGHT_CUBE,
        remote_name=REMOTE_NAME,
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        db_session=db_session,
        transport=transport,
    )


async def _make_unknown_call(
    db_session, user_id: str, run: AgentRun, step: AgentStep, *, upstream_request_id: str
) -> tuple[str, AgentToolCall]:
    args_hash = hashlib.sha256(canonical_json_bytes({"keyword": "美妆"})).hexdigest()
    logical_id = logical_call_id_for(run.id, step.id, INTERNAL_NAME, args_hash)
    now = utc_now()
    call = AgentToolCall(
        id=str(uuid4()),
        run_id=run.id,
        step_id=step.id,
        logical_call_id=logical_id,
        service=DataTapService.INSIGHT_CUBE.value,
        internal_tool_name=INTERNAL_NAME,
        arguments_json={"keyword": "美妆"},
        arguments_hash=args_hash,
        status="unknown",
        points_reserved=MCP_POINTS_COST,
        upstream_request_id=upstream_request_id,
        started_at=now,
    )
    db_session.add(call)
    await WalletService(db_session).reserve(
        user_id, MCP_POINTS_COST, f"agent-mcp:{logical_id}:reserve", call.id,
        reference_type="agent_tool_call",
    )
    await db_session.flush()
    return logical_id, call


async def _funded_user(db_session, user_factory):
    user = await user_factory()
    await WalletService(db_session).ensure_welcome_grant(user.id)
    return user


async def _reconciliation(db_session, call: AgentToolCall) -> AgentToolCallReconciliation | None:
    return await db_session.scalar(
        select(AgentToolCallReconciliation).where(
            AgentToolCallReconciliation.tool_call_id == call.id
        )
    )


def _make_recovery(db_session, *, executor, transport: FakeMcpTransport) -> RecoveryLoop:
    return RecoveryLoop(
        executor=executor,
        session_factory=lambda: _shared_session(db_session),
        tool_factory=lambda _db, _call: _bridge(db_session, transport),
        worker_id="recovery-worker",
        lease_seconds=300,
        interval_seconds=0.01,
        clock=utc_now,
    )


async def _make_executor(db_session, *, worker: str = "recovery-worker") -> AgentRunExecutor:
    gateway = FakeAgentGateway([Complete(action="complete", text="恢复完成")])
    broker = AgentEventBroker()
    events = AgentEventStream(db_session, broker)
    reviewer = ReviewerDriver(db_session, _FakeReviewerGateway(), worker_id=worker)

    def engine_factory(db):
        return AgentEngine(
            db, gateway=gateway, registry=ToolRegistry(), events=events,
            reviewer=reviewer, worker_id=worker,
        )

    return AgentRunExecutor(
        session_factory=lambda: _shared_session(db_session),
        engine_factory=engine_factory,
        worker_id=worker,
        claim_interval_seconds=0.01,
    )


# ---------------------------------------------------------------------------
# 1. 过期租约恢复：新 attempt + 引擎继续
# ---------------------------------------------------------------------------


async def test_recovery_reclaims_expired_lease_run(db_session, user_factory) -> None:
    user = await _funded_user(db_session, user_factory)
    session, run, step = await _make_chain(db_session, user.id)
    # 已有 attempt 1 且租约过期
    row = await db_session.get(AgentRun, run.id)
    row.lease_expires_at = utc_now() - timedelta(seconds=10)
    await db_session.flush()

    executor = await _make_executor(db_session)
    recovery = _make_recovery(db_session, executor=executor, transport=FakeMcpTransport())

    reclaimed = await recovery.reclaim_expired_runs()

    assert run.id in reclaimed
    fresh = await db_session.get(AgentRun, run.id)
    assert fresh.status == RunStatus.COMPLETED
    assert fresh.lease_owner is None
    attempts = list(
        (
            await db_session.scalars(
                select(AgentRunAttempt)
                .where(AgentRunAttempt.run_id == run.id)
                .order_by(AgentRunAttempt.attempt)
            )
        ).all()
    )
    assert [attempt.attempt for attempt in attempts] == [1, 2]
    assert attempts[1].outcome == "completed"


async def test_recovery_skips_run_with_unexpired_lease(db_session, user_factory) -> None:
    user = await _funded_user(db_session, user_factory)
    session, run, step = await _make_chain(db_session, user.id)
    row = await db_session.get(AgentRun, run.id)
    row.lease_expires_at = utc_now() + timedelta(seconds=300)
    await db_session.flush()

    executor = await _make_executor(db_session)
    recovery = _make_recovery(db_session, executor=executor, transport=FakeMcpTransport())

    reclaimed = await recovery.reclaim_expired_runs()
    assert run.id not in reclaimed
    fresh = await db_session.get(AgentRun, run.id)
    assert fresh.status == RunStatus.RUNNING


# ---------------------------------------------------------------------------
# 2. unknown MCP 恢复核对：按 logical_call_id，绝不重发原工具
# ---------------------------------------------------------------------------


async def test_reconcile_confirmed_success_with_payload_settles_and_writes_evidence(
    db_session, user_factory
) -> None:
    user = await _funded_user(db_session, user_factory)
    session, run, step = await _make_chain(db_session, user.id)
    logical_id, call = await _make_unknown_call(db_session, user.id, run, step, upstream_request_id="req-1")
    transport = FakeMcpTransport()
    transport.reconciled["req-1"] = RemoteToolResult(
        structured_content={"result": "ok"}, is_error=False, upstream_request_id="req-1"
    )
    executor = await _make_executor(db_session)
    recovery = _make_recovery(db_session, executor=executor, transport=transport)

    reconciled, warnings = await recovery.reconcile_unknown_calls()

    assert logical_id in reconciled
    assert warnings == ()
    # 绝不重发原工具
    assert transport.calls == []
    fresh = await db_session.get(AgentToolCall, call.id)
    assert fresh.status == "settled"
    assert fresh.points_settled == MCP_POINTS_COST
    wallet = await WalletService(db_session).get_wallet(user.id)
    assert (wallet.balance, wallet.reserved) == (990, 0)
    evidence = await db_session.scalar(select(EvidenceItem).where(EvidenceItem.tool_call_id == call.id))
    assert evidence is not None
    assert evidence.raw_payload_json == {"result": "ok"}
    recon = await _reconciliation(db_session, call)
    assert recon is not None
    assert recon.decision == "confirm_success"
    assert recon.source == "upstream_probe"


async def test_reconcile_confirmed_success_without_payload_settles_result_unavailable_no_evidence(
    db_session, user_factory
) -> None:
    user = await _funded_user(db_session, user_factory)
    session, run, step = await _make_chain(db_session, user.id)
    logical_id, call = await _make_unknown_call(db_session, user.id, run, step, upstream_request_id="req-1")
    transport = FakeMcpTransport()
    transport.reconciled["req-1"] = RemoteToolResult(
        structured_content=None, is_error=False, upstream_request_id="req-1"
    )
    executor = await _make_executor(db_session)
    recovery = _make_recovery(db_session, executor=executor, transport=transport)

    reconciled, warnings = await recovery.reconcile_unknown_calls()

    assert logical_id in reconciled
    assert transport.calls == []
    fresh = await db_session.get(AgentToolCall, call.id)
    assert fresh.status == "settled"
    assert fresh.points_settled == MCP_POINTS_COST
    assert fresh.safe_error_message == "result_unavailable"
    # 无可回取 payload：不得生成 Evidence
    assert (
        await db_session.scalar(select(EvidenceItem).where(EvidenceItem.tool_call_id == call.id))
    ) is None
    wallet = await WalletService(db_session).get_wallet(user.id)
    assert (wallet.balance, wallet.reserved) == (990, 0)
    recon = await _reconciliation(db_session, call)
    assert recon.decision == "confirm_success"


async def test_reconcile_confirmed_failure_releases_reservation(db_session, user_factory) -> None:
    user = await _funded_user(db_session, user_factory)
    session, run, step = await _make_chain(db_session, user.id)
    logical_id, call = await _make_unknown_call(db_session, user.id, run, step, upstream_request_id="req-1")
    transport = FakeMcpTransport()
    transport.reconciled["req-1"] = RemoteToolResult(
        structured_content=None, is_error=True, upstream_request_id="req-1", error_text="failed"
    )
    executor = await _make_executor(db_session)
    recovery = _make_recovery(db_session, executor=executor, transport=transport)

    reconciled, warnings = await recovery.reconcile_unknown_calls()

    assert logical_id in reconciled
    assert transport.calls == []
    fresh = await db_session.get(AgentToolCall, call.id)
    assert fresh.status == "failed"
    assert fresh.points_reserved == 0
    wallet = await WalletService(db_session).get_wallet(user.id)
    assert (wallet.balance, wallet.reserved) == (1000, 0)
    recon = await _reconciliation(db_session, call)
    assert recon.decision == "confirm_failure"


async def test_reconcile_unconfirmable_keeps_reserved_unknown_and_warns(
    db_session, user_factory
) -> None:
    user = await _funded_user(db_session, user_factory)
    session, run, step = await _make_chain(db_session, user.id)
    logical_id, call = await _make_unknown_call(db_session, user.id, run, step, upstream_request_id="req-1")
    transport = FakeMcpTransport()  # 无 reconcile 结果 → 无法核对
    executor = await _make_executor(db_session)
    recovery = _make_recovery(db_session, executor=executor, transport=transport)

    reconciled, warnings = await recovery.reconcile_unknown_calls()

    assert logical_id in reconciled  # 本轮已尝试核对
    assert warnings == (logical_id,)  # 无法核对 → 运维告警
    assert transport.calls == []
    # 保持预留：不被超时自动释放
    fresh = await db_session.get(AgentToolCall, call.id)
    assert fresh.status == "unknown"
    assert fresh.points_reserved == MCP_POINTS_COST
    wallet = await WalletService(db_session).get_wallet(user.id)
    assert (wallet.balance, wallet.reserved) == (990, 10)
    recon = await _reconciliation(db_session, call)
    assert recon is not None
    assert recon.decision == "keep_unknown"
    assert recon.source == "upstream_probe"


async def test_reconcile_does_not_auto_release_old_unknown_by_timeout(db_session, user_factory) -> None:
    """unknown 不沿用旧 release_expired_unknown 的超时自动释放策略。"""
    user = await _funded_user(db_session, user_factory)
    session, run, step = await _make_chain(db_session, user.id)
    logical_id, call = await _make_unknown_call(db_session, user.id, run, step, upstream_request_id="req-1")
    # 调用早已完成很久（远大于任何 observation 窗口）
    fresh = await db_session.get(AgentToolCall, call.id)
    fresh.completed_at = utc_now() - timedelta(days=1)
    await db_session.flush()
    transport = FakeMcpTransport()
    executor = await _make_executor(db_session)
    recovery = _make_recovery(db_session, executor=executor, transport=transport)

    await recovery.reconcile_unknown_calls()

    fresh = await db_session.get(AgentToolCall, call.id)
    assert fresh.status == "unknown"
    assert fresh.points_reserved == MCP_POINTS_COST
    wallet = await WalletService(db_session).get_wallet(user.id)
    assert wallet.reserved == MCP_POINTS_COST
