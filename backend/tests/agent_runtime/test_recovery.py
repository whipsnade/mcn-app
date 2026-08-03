"""RecoveryLoop 集成测试（Task 15 / 设计文档 §11.1 / §11.2 / v3 加固 §5.4）。

覆盖：
1. 过期租约 Run 被恢复循环领取重建 Attempt，引擎继续执行到终态；
2. unknown MCP 调用只经 AgentMcpTool.reconcile 按 logical_call_id 只读核对，
   绝不重发原工具；四种结果（确认成功带 payload / 确认成功无 payload /
   确认失败 / 无法核对）分别 settle / release / 保持预留；
3. 无法核对的 unknown 保持 reserved + keep_unknown 审计，并产生运维告警；
   不使用旧的 release_expired_unknown 超时自动释放策略；
4. 恢复核对结果必须提交（独立会话读回可见），而非在会话关闭时回滚；
5. 同一 unconfirmable 调用多次扫描只追加一条 keep_unknown 审计；
6. 恢复循环使用独立 worker id，与原 worker 租约隔离；
7. 超过受控时间仍处于 running/reserved 的调用先迁移为 unknown 再走只读核对
   （§5.4），绝不直接释放或重新外发。
"""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, Mapping
from uuid import uuid4

from sqlalchemy import select

from app.agent_runtime.engine import AgentEngine
from app.agent_runtime.events import (
    AgentEventBroker,
    AgentEventStream,
    is_terminal_event,
)
from app.agent_runtime.executor import AgentRunExecutor
from app.agent_runtime.models import (
    AgentEvent,
    AgentRun,
    AgentRunAttempt,
    AgentSession,
    AgentStep,
    AgentToolCall,
    AgentToolCallReconciliation,
    EvidenceItem,
)
from app.agent_runtime.recovery import RecoveryLoop
from app.agent_runtime.repository import AgentRunRepository, utc_now
from app.agent_runtime.reviewer import ReviewerDriver
from app.agent_runtime.schemas import Complete
from app.agent_runtime.state import RunStatus
from app.agent_runtime.tools.mcp import MCP_POINTS_COST, AgentMcpTool, logical_call_id_for
from app.agent_runtime.tools.registry import ToolRegistry
from app.billing.models import Wallet
from app.billing.service import WalletService
from app.db.session import SessionFactory
from app.identity.models import User
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
        session_factory=lambda: _shared_session(db_session),
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


def _make_recovery(db_session, *, executor, transport: FakeMcpTransport, stuck_seconds: float = 900.0) -> RecoveryLoop:
    return RecoveryLoop(
        executor=executor,
        session_factory=lambda: _shared_session(db_session),
        tool_factory=lambda _db, _call: _bridge(db_session, transport),
        worker_id="recovery-worker",
        lease_seconds=300,
        interval_seconds=0.01,
        clock=utc_now,
        stuck_seconds=stuck_seconds,
    )


async def _make_executor(
    db_session, *, worker: str = "exec-worker", gateway: FakeAgentGateway | None = None
) -> AgentRunExecutor:
    if gateway is None:
        gateway = FakeAgentGateway([Complete(action="complete", text="恢复完成")])
    broker = AgentEventBroker()
    events = AgentEventStream(db_session, broker)
    reviewer = ReviewerDriver(db_session, _FakeReviewerGateway(), worker_id=worker)

    def engine_factory(db, worker_id, channel_permissions=()):
        return AgentEngine(
            db, gateway=gateway, registry=ToolRegistry(), events=events,
            reviewer=reviewer, worker_id=worker_id,
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


async def test_recovery_reclaims_null_lease_running_run(db_session, user_factory) -> None:
    """无租约（NULL）running Run 也应被恢复循环接管（与执行器 _find_claimable_id 一致）。

    NULL 租约是 API resume 经 ``begin_attempt(resumed=True)`` 留下的状态；
    执行器停止时若恢复循环也不接管会永久卡在 running。
    """
    user = await _funded_user(db_session, user_factory)
    session, run, step = await _make_chain(db_session, user.id)
    row = await db_session.get(AgentRun, run.id)
    assert row.lease_expires_at is None  # _make_chain 未设租约

    executor = await _make_executor(db_session)
    recovery = _make_recovery(db_session, executor=executor, transport=FakeMcpTransport())

    reclaimed = await recovery.reclaim_expired_runs()

    assert run.id in reclaimed
    fresh = await db_session.get(AgentRun, run.id)
    assert fresh.status == RunStatus.COMPLETED
    assert fresh.lease_owner is None


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


# ---------------------------------------------------------------------------
# 4. 恢复核对必须提交：独立会话读回可见（Fix 1）
# ---------------------------------------------------------------------------


async def _setup_call_committed(
    *,
    status: str = "unknown",
    upstream_request_id: str | None = "req-1",
    started_at=None,
):
    """在独立已提交事务中创建调用链（默认 unknown），供恢复循环（真实 SessionFactory）读取。"""
    now = utc_now()
    async with SessionFactory.begin() as db:
        user = User(
            id=str(uuid4()), nickname="恢复提交用户", role="user",
            status="active", created_at=now, updated_at=now,
        )
        db.add(user)
        await db.flush()
        db.add(Wallet(user_id=user.id, balance=1000, reserved=0, version=0, updated_at=now))
        session = AgentSession(
            id=str(uuid4()), user_id=user.id, title="恢复提交会话", status="active",
            created_at=now, updated_at=now,
        )
        db.add(session)
        # agent_runs 与 agent_messages 存在环形外键（run_id / input_message_id），
        # 同一 flush 内的一次性插入可能破坏拓扑排序；逐条 flush 保证父行先落库。
        await db.flush()
        run = AgentRun(
            id=str(uuid4()), session_id=session.id, user_id=user.id,
            run_kind="user", visibility="user",
            profile_name="session_analyst_v1", profile_version="v1", model="test-model",
            status="running", decision_count=0, review_count=0, revision_count=0,
            started_at=now,
        )
        db.add(run)
        await db.flush()
        attempt = AgentRunAttempt(
            id=str(uuid4()), run_id=run.id, attempt=1, started_at=now,
            decision_count=0, outcome="running",
        )
        db.add(attempt)
        await db.flush()
        step = AgentStep(
            id=str(uuid4()), run_id=run.id, attempt_id=attempt.id, sequence=1,
            step_type="tool_call", status="running", visibility="user", created_at=now,
        )
        db.add(step)
        await db.flush()
        args_hash = hashlib.sha256(canonical_json_bytes({"keyword": "美妆"})).hexdigest()
        logical_id = logical_call_id_for(run.id, step.id, INTERNAL_NAME, args_hash)
        call = AgentToolCall(
            id=str(uuid4()),
            run_id=run.id,
            step_id=step.id,
            logical_call_id=logical_id,
            service=DataTapService.INSIGHT_CUBE.value,
            internal_tool_name=INTERNAL_NAME,
            arguments_json={"keyword": "美妆"},
            arguments_hash=args_hash,
            status=status,
            points_reserved=MCP_POINTS_COST,
            upstream_request_id=upstream_request_id,
            started_at=started_at if started_at is not None else now,
        )
        db.add(call)
        await db.flush()
        await WalletService(db).reserve(
            user.id, MCP_POINTS_COST, f"agent-mcp:{logical_id}:reserve", call.id,
            reference_type="agent_tool_call",
        )
        await db.flush()
        user_id, call_id = user.id, call.id
    return user_id, call_id, logical_id


async def _setup_unknown_call_committed(*, upstream_request_id: str = "req-1"):
    """在独立已提交事务中创建 unknown 调用，供恢复循环（真实 SessionFactory）读取。"""
    return await _setup_call_committed(upstream_request_id=upstream_request_id)


async def _cleanup_committed_user(user_id: str, call_id: str) -> None:
    """删除独立事务中提交的测试链（agent_tool_calls.step_id 无级联，须先删调用子行）。"""
    async with SessionFactory() as db:
        for row in (
            await db.scalars(
                select(AgentToolCallReconciliation).where(
                    AgentToolCallReconciliation.tool_call_id == call_id
                )
            )
        ).all():
            await db.delete(row)
        for row in (
            await db.scalars(select(EvidenceItem).where(EvidenceItem.tool_call_id == call_id))
        ).all():
            await db.delete(row)
        call = await db.get(AgentToolCall, call_id)
        if call is not None:
            await db.delete(call)
        # 其余（run/attempt/step/session/wallet/流水）由 users 的级联删除收尾。
        user = await db.get(User, user_id)
        if user is not None:
            await db.delete(user)
        await db.commit()


async def test_reconcile_writes_committed_visible_from_fresh_session(
    db_session, user_factory
) -> None:
    """恢复核对结果必须提交：用全新独立会话读回 settle/evidence/审计/钱包。"""
    transport = FakeMcpTransport()
    transport.reconciled["req-1"] = RemoteToolResult(
        structured_content={"result": "ok"}, is_error=False, upstream_request_id="req-1"
    )
    user_id, call_id, logical_id = await _setup_unknown_call_committed(upstream_request_id="req-1")
    try:
        executor = await _make_executor(db_session)
        recovery = RecoveryLoop(
            executor=executor,
            session_factory=SessionFactory,
            tool_factory=lambda db, _call: _bridge(db, transport),
            worker_id="recovery-worker",
            lease_seconds=300,
            interval_seconds=0.01,
            clock=utc_now,
        )

        reconciled, warnings = await recovery.reconcile_unknown_calls()

        assert logical_id in reconciled
        assert warnings == ()
        # 全新独立会话读回：若 reconcile 未提交，这里什么都看不到（生产 no-op）
        async with SessionFactory() as db:
            call = await db.get(AgentToolCall, call_id)
            assert call is not None
            assert call.status == "settled"
            assert call.points_settled == MCP_POINTS_COST
            evidence = await db.scalar(select(EvidenceItem).where(EvidenceItem.tool_call_id == call_id))
            assert evidence is not None
            recon = await db.scalar(
                select(AgentToolCallReconciliation).where(
                    AgentToolCallReconciliation.tool_call_id == call_id
                )
            )
            assert recon is not None
            assert recon.decision == "confirm_success"
            wallet = await db.get(Wallet, user_id)
            assert wallet is not None
            assert (wallet.balance, wallet.reserved) == (990, 0)
    finally:
        # 独立事务提交的数据会跨测试残留，必须清理避免污染后续用例。
        await _cleanup_committed_user(user_id, call_id)


# ---------------------------------------------------------------------------
# 5. keep_unknown 审计去重：多次扫描只记一条（Fix 2）
# ---------------------------------------------------------------------------


async def test_reconcile_unconfirmable_dedupes_keep_unknown_audit(
    db_session, user_factory
) -> None:
    """同一 unconfirmable 调用多次扫描：只追加一条 keep_unknown，告警只在首次。"""
    user = await _funded_user(db_session, user_factory)
    session, run, step = await _make_chain(db_session, user.id)
    logical_id, call = await _make_unknown_call(db_session, user.id, run, step, upstream_request_id="req-1")
    transport = FakeMcpTransport()  # 无 reconcile 结果 → 无法核对
    executor = await _make_executor(db_session)
    recovery = _make_recovery(db_session, executor=executor, transport=transport)

    first_reconciled, first_warnings = await recovery.reconcile_unknown_calls()
    second_reconciled, second_warnings = await recovery.reconcile_unknown_calls()

    assert logical_id in first_reconciled
    assert first_warnings == (logical_id,)  # 首次状态变化 → 告警
    assert logical_id in second_reconciled  # 每轮仍探测 transport
    assert second_warnings == ()  # 状态未变化 → 不再告警
    assert transport.calls == []  # 始终只读核对，绝不重发
    rows = list(
        (
            await db_session.scalars(
                select(AgentToolCallReconciliation).where(
                    AgentToolCallReconciliation.tool_call_id == call.id
                )
            )
        ).all()
    )
    assert len(rows) == 1
    assert rows[0].decision == "keep_unknown"


# ---------------------------------------------------------------------------
# 6. 恢复循环使用独立 worker id（Fix 4）
# ---------------------------------------------------------------------------


async def test_recovery_reclaim_uses_distinct_worker_id(db_session, user_factory) -> None:
    """恢复领取重建使用 recovery 自己的 worker id，与原 worker 租约隔离。"""
    user = await _funded_user(db_session, user_factory)
    session, run, step = await _make_chain(db_session, user.id)
    row = await db_session.get(AgentRun, run.id)
    row.lease_expires_at = utc_now() - timedelta(seconds=10)
    await db_session.flush()
    seen_worker_ids: list[str] = []

    def engine_factory(db, worker_id, channel_permissions=()):
        seen_worker_ids.append(worker_id)
        gateway = FakeAgentGateway([Complete(action="complete", text="恢复完成")])
        broker = AgentEventBroker()
        events = AgentEventStream(db, broker)
        reviewer = ReviewerDriver(db, _FakeReviewerGateway(), worker_id=worker_id)
        return AgentEngine(
            db, gateway=gateway, registry=ToolRegistry(), events=events,
            reviewer=reviewer, worker_id=worker_id,
        )

    executor = AgentRunExecutor(
        session_factory=lambda: _shared_session(db_session),
        engine_factory=engine_factory,
        worker_id="exec-worker",
        claim_interval_seconds=0.01,
    )
    recovery = RecoveryLoop(
        executor=executor,
        session_factory=lambda: _shared_session(db_session),
        tool_factory=lambda _db, _call: None,
        worker_id="recovery-worker",
        lease_seconds=300,
        interval_seconds=0.01,
        clock=utc_now,
    )
    assert executor._worker_id != recovery._worker_id

    reclaimed = await recovery.reclaim_expired_runs()

    assert run.id in reclaimed
    # 恢复处理使用 recovery 独立 worker（不是 executor 的 worker）
    assert seen_worker_ids == ["recovery-worker"]
    fresh = await db_session.get(AgentRun, run.id)
    assert fresh.status == RunStatus.COMPLETED


# ---------------------------------------------------------------------------
# 7. stuck running/reserved 调用：先迁移 unknown 再核对，绝不直接释放或重发（§5.4）
# ---------------------------------------------------------------------------


async def _make_call(
    db_session,
    user_id: str,
    run: AgentRun,
    step: AgentStep,
    *,
    status: str,
    upstream_request_id: str | None,
    started_at,
) -> tuple[str, AgentToolCall]:
    """任意状态的调用行 + 10 分预留（模拟 prepare 已提交后的各种崩溃残留）。"""
    args_hash = hashlib.sha256(canonical_json_bytes({"keyword": "美妆"})).hexdigest()
    logical_id = logical_call_id_for(run.id, step.id, INTERNAL_NAME, args_hash)
    call = AgentToolCall(
        id=str(uuid4()),
        run_id=run.id,
        step_id=step.id,
        logical_call_id=logical_id,
        service=DataTapService.INSIGHT_CUBE.value,
        internal_tool_name=INTERNAL_NAME,
        arguments_json={"keyword": "美妆"},
        arguments_hash=args_hash,
        status=status,
        points_reserved=MCP_POINTS_COST,
        upstream_request_id=upstream_request_id,
        started_at=started_at,
    )
    db_session.add(call)
    await WalletService(db_session).reserve(
        user_id, MCP_POINTS_COST, f"agent-mcp:{logical_id}:reserve", call.id,
        reference_type="agent_tool_call",
    )
    await db_session.flush()
    return logical_id, call


async def test_stuck_running_call_migrates_to_unknown_then_reconciles(
    db_session, user_factory
) -> None:
    """外发后崩溃残留的 running 调用（超过受控时间）→ 迁移 unknown → 只读核对 settle。"""
    user = await _funded_user(db_session, user_factory)
    session, run, step = await _make_chain(db_session, user.id)
    logical_id, call = await _make_call(
        db_session, user.id, run, step,
        status="running",
        upstream_request_id="req-stuck",
        started_at=utc_now() - timedelta(hours=1),
    )
    transport = FakeMcpTransport()
    transport.reconciled["req-stuck"] = RemoteToolResult(
        structured_content={"result": "ok"}, is_error=False, upstream_request_id="req-stuck"
    )
    executor = await _make_executor(db_session)
    recovery = _make_recovery(db_session, executor=executor, transport=transport, stuck_seconds=60)

    migrated = await recovery.migrate_stuck_tool_calls()

    assert migrated == (logical_id,)
    fresh = await db_session.get(AgentToolCall, call.id)
    assert fresh.status == "unknown"
    assert fresh.error_type == "result_unknown"
    # 迁移只改状态：预留保留，绝不直接释放
    assert fresh.points_reserved == MCP_POINTS_COST
    wallet = await WalletService(db_session).get_wallet(user.id)
    assert (wallet.balance, wallet.reserved) == (990, 10)

    reconciled, warnings = await recovery.reconcile_unknown_calls()

    assert logical_id in reconciled
    assert warnings == ()
    assert transport.calls == []  # 绝不重新外发
    fresh = await db_session.get(AgentToolCall, call.id)
    assert fresh.status == "settled"
    assert fresh.points_settled == MCP_POINTS_COST
    evidence = await db_session.scalar(
        select(EvidenceItem).where(EvidenceItem.tool_call_id == call.id)
    )
    assert evidence is not None
    wallet = await WalletService(db_session).get_wallet(user.id)
    assert (wallet.balance, wallet.reserved) == (990, 0)


async def test_run_once_migrates_stuck_then_reconciles_in_same_round(
    db_session, user_factory
) -> None:
    """run_once 单轮内完成 stuck 迁移 + 核对；返回四元组含迁移清单。"""
    user = await _funded_user(db_session, user_factory)
    session, run, step = await _make_chain(db_session, user.id)
    # Run 租约未过期：本轮不触发 Run 接管，隔离断言 stuck 迁移路径
    run_row = await db_session.get(AgentRun, run.id)
    run_row.lease_expires_at = utc_now() + timedelta(seconds=300)
    await db_session.flush()
    logical_id, call = await _make_call(
        db_session, user.id, run, step,
        status="running",
        upstream_request_id="req-stuck",
        started_at=utc_now() - timedelta(hours=1),
    )
    transport = FakeMcpTransport()
    transport.reconciled["req-stuck"] = RemoteToolResult(
        structured_content={"result": "ok"}, is_error=False, upstream_request_id="req-stuck"
    )
    executor = await _make_executor(db_session)
    recovery = _make_recovery(db_session, executor=executor, transport=transport, stuck_seconds=60)

    reclaimed, stuck, reconciled, warnings = await recovery.run_once()

    assert reclaimed == ()
    assert stuck == (logical_id,)
    assert logical_id in reconciled
    assert warnings == ()
    fresh = await db_session.get(AgentToolCall, call.id)
    assert fresh.status == "settled"


async def test_fresh_running_call_is_not_migrated(db_session, user_factory) -> None:
    """受控时间内的 running 调用（正常在途）不得迁移。"""
    user = await _funded_user(db_session, user_factory)
    session, run, step = await _make_chain(db_session, user.id)
    logical_id, call = await _make_call(
        db_session, user.id, run, step,
        status="running",
        upstream_request_id=None,
        started_at=utc_now(),
    )
    executor = await _make_executor(db_session)
    recovery = _make_recovery(db_session, executor=executor, transport=FakeMcpTransport())

    migrated = await recovery.migrate_stuck_tool_calls()

    assert migrated == ()
    fresh = await db_session.get(AgentToolCall, call.id)
    assert fresh.status == "running"
    assert fresh.points_reserved == MCP_POINTS_COST


async def test_stuck_reserved_call_without_started_at_migrates_to_unknown(
    db_session, user_factory
) -> None:
    """reserved 且无 started_at（旧两阶段流程崩溃残留）按 stuck 迁移为 unknown。"""
    user = await _funded_user(db_session, user_factory)
    session, run, step = await _make_chain(db_session, user.id)
    logical_id, call = await _make_call(
        db_session, user.id, run, step,
        status="reserved",
        upstream_request_id=None,
        started_at=None,
    )
    executor = await _make_executor(db_session)
    recovery = _make_recovery(db_session, executor=executor, transport=FakeMcpTransport())

    migrated = await recovery.migrate_stuck_tool_calls()

    assert migrated == (logical_id,)
    fresh = await db_session.get(AgentToolCall, call.id)
    assert fresh.status == "unknown"
    # 绝不直接释放：预留保留等待核对
    assert fresh.points_reserved == MCP_POINTS_COST
    wallet = await WalletService(db_session).get_wallet(user.id)
    assert (wallet.balance, wallet.reserved) == (990, 10)


async def test_stuck_migration_is_atomic_against_concurrent_finalize(
    db_session, user_factory
) -> None:
    """迁移用条件 UPDATE 守护：已终态（settled/failed）的行不会被翻回 unknown。"""
    user = await _funded_user(db_session, user_factory)
    session, run, step = await _make_chain(db_session, user.id)
    logical_id, call = await _make_call(
        db_session, user.id, run, step,
        status="running",
        upstream_request_id="req-stuck",
        started_at=utc_now() - timedelta(hours=1),
    )
    # 扫描读到 running 之后、迁移之前，调用已被 finalize 为 settled
    await WalletService(db_session).settle(
        user.id, MCP_POINTS_COST, f"agent-mcp:{logical_id}:settle", call.id,
        reference_type="agent_tool_call",
    )
    settled = await db_session.get(AgentToolCall, call.id)
    settled.status = "settled"
    settled.points_reserved = 0
    settled.points_settled = MCP_POINTS_COST
    await db_session.flush()
    executor = await _make_executor(db_session)
    recovery = _make_recovery(db_session, executor=executor, transport=FakeMcpTransport())

    migrated = await recovery.migrate_stuck_tool_calls()

    assert migrated == ()
    fresh = await db_session.get(AgentToolCall, call.id)
    assert fresh.status == "settled"
    wallet = await WalletService(db_session).get_wallet(user.id)
    assert (wallet.balance, wallet.reserved) == (990, 0)


async def test_stuck_running_call_recovers_via_committed_sessions(
    db_session, user_factory
) -> None:
    """真实崩溃场景（真实 SessionFactory 独立连接）：外发后未 finalize 的 running 行
    被恢复扫描迁移为 unknown 并核对 settle，结果对全新会话可见。"""
    transport = FakeMcpTransport()
    transport.reconciled["req-stuck"] = RemoteToolResult(
        structured_content={"result": "ok"}, is_error=False, upstream_request_id="req-stuck"
    )
    user_id, call_id, logical_id = await _setup_call_committed(
        status="running",
        upstream_request_id="req-stuck",
        started_at=utc_now() - timedelta(hours=1),
    )
    try:
        executor = await _make_executor(db_session)
        recovery = RecoveryLoop(
            executor=executor,
            session_factory=SessionFactory,
            tool_factory=lambda db, _call: _bridge(db, transport),
            worker_id="recovery-worker",
            lease_seconds=300,
            interval_seconds=0.01,
            clock=utc_now,
            stuck_seconds=60,
        )

        migrated = await recovery.migrate_stuck_tool_calls()
        reconciled, warnings = await recovery.reconcile_unknown_calls()

        assert migrated == (logical_id,)
        assert logical_id in reconciled
        assert warnings == ()
        assert transport.calls == []
        async with SessionFactory() as db:
            call = await db.get(AgentToolCall, call_id)
            assert call.status == "settled"
            assert call.points_settled == MCP_POINTS_COST
            evidence = await db.scalar(
                select(EvidenceItem).where(EvidenceItem.tool_call_id == call_id)
            )
            assert evidence is not None
            wallet = await db.get(Wallet, user_id)
            assert (wallet.balance, wallet.reserved) == (990, 0)
    finally:
        await _cleanup_committed_user(user_id, call_id)


# ---------------------------------------------------------------------------
# 8. 取消待处理孤儿 Run 收口（I1：崩溃时取消待处理的恢复收口）
# ---------------------------------------------------------------------------


async def _terminal_event_types(db_session, run_id: str) -> list[str]:
    rows = (
        await db_session.scalars(
            select(AgentEvent)
            .where(AgentEvent.run_id == run_id)
            .order_by(AgentEvent.sequence)
        )
    ).all()
    return [row.event_type for row in rows if is_terminal_event(row.event_type)]


async def test_recovery_settles_cancel_requested_orphan_running(
    db_session, user_factory
) -> None:
    """running + cancel_requested + 租约过期（API 写入取消后进程崩溃）：
    恢复循环不恢复模型执行，直接收口 cancelled 并发恰好一个 run.cancelled。
    """
    user = await _funded_user(db_session, user_factory)
    session, run, step = await _make_chain(db_session, user.id)
    row = await db_session.get(AgentRun, run.id)
    row.cancel_requested = True
    row.lease_owner = "dead-worker"
    row.lease_expires_at = utc_now() - timedelta(seconds=10)
    await db_session.flush()
    gateway = FakeAgentGateway([Complete(action="complete", text="不应执行")])
    executor = await _make_executor(db_session, gateway=gateway)
    recovery = _make_recovery(db_session, executor=executor, transport=FakeMcpTransport())

    reclaimed = await recovery.reclaim_expired_runs()

    assert run.id in reclaimed
    fresh = await db_session.get(AgentRun, run.id)
    assert fresh.status == RunStatus.CANCELLED
    assert fresh.completed_at is not None
    assert fresh.lease_owner is None
    assert fresh.lease_expires_at is None
    # 不恢复模型执行：引擎/模型从未被调用
    assert gateway.calls == []
    # 恰好一个 run.cancelled 终态事件
    assert await _terminal_event_types(db_session, run.id) == ["run.cancelled"]
    # open Attempt 以 cancelled 收口（不遗留 running attempt）
    attempt = await db_session.scalar(
        select(AgentRunAttempt).where(AgentRunAttempt.run_id == run.id)
    )
    assert attempt is not None
    assert attempt.outcome == "cancelled"
    assert attempt.ended_at is not None


async def test_recovery_settles_cancel_requested_orphan_reviewing_releases_draft(
    db_session, user_factory
) -> None:
    """reviewing + cancel_requested + 租约过期（复核期间取消后崩溃）：
    收口 cancelled，同时释放本 Run 持有的 Draft working head（idle），
    不遗留 artifact_busy；Review Batch 保持取消口径的既有语义（不整批 failed）。
    """
    from app.agent_artifacts.models import ArtifactDraft, ArtifactReviewBatch

    from tests.agent_runtime.test_engine import _make_draft

    user = await _funded_user(db_session, user_factory)
    session, run, step = await _make_chain(db_session, user.id)
    service, draft, _revision = await _make_draft(db_session, run)
    reviewer = ReviewerDriver(db_session, _FakeReviewerGateway(), worker_id="dead-worker")
    batch = await reviewer.create_batch(
        parent_run_id=run.id, draft_ids=(draft.id,), completion_text="分析完成"
    )
    row = await db_session.get(AgentRun, run.id)
    row.status = RunStatus.REVIEWING
    row.cancel_requested = True
    row.lease_owner = "dead-worker"
    row.lease_expires_at = utc_now() - timedelta(seconds=10)
    await db_session.flush()
    gateway = FakeAgentGateway([Complete(action="complete", text="不应执行")])
    executor = await _make_executor(db_session, gateway=gateway)
    recovery = _make_recovery(db_session, executor=executor, transport=FakeMcpTransport())

    reclaimed = await recovery.reclaim_expired_runs()

    assert run.id in reclaimed
    fresh = await db_session.get(AgentRun, run.id)
    assert fresh.status == RunStatus.CANCELLED
    assert gateway.calls == []
    assert await _terminal_event_types(db_session, run.id) == ["run.cancelled"]
    # Draft working head 释放（idle + owner 清空），后续 Run 可立即接管
    fresh_draft = await db_session.get(ArtifactDraft, draft.id)
    assert fresh_draft.status == "idle"
    assert fresh_draft.owner_run_id is None
    # 取消口径与在线路径一致：不套用 reject 的整批 failed 清理
    fresh_batch = await db_session.get(ArtifactReviewBatch, batch.id)
    assert fresh_batch.status == "pending"


async def test_recovery_skips_cancel_requested_run_with_active_lease(
    db_session, user_factory
) -> None:
    """cancel_requested 但租约仍被活跃持有：在途 worker 的取消检查点会自行
    收口，恢复循环不抢（A4 语义）——不迁移、不发终态事件。"""
    user = await _funded_user(db_session, user_factory)
    session, run, step = await _make_chain(db_session, user.id)
    row = await db_session.get(AgentRun, run.id)
    row.cancel_requested = True
    row.lease_owner = "worker-a"
    row.lease_expires_at = utc_now() + timedelta(seconds=300)
    await db_session.flush()
    gateway = FakeAgentGateway([Complete(action="complete", text="不应执行")])
    executor = await _make_executor(db_session, gateway=gateway)
    recovery = _make_recovery(db_session, executor=executor, transport=FakeMcpTransport())

    reclaimed = await recovery.reclaim_expired_runs()

    assert run.id not in reclaimed
    fresh = await db_session.get(AgentRun, run.id)
    assert fresh.status == RunStatus.RUNNING
    assert fresh.lease_owner == "worker-a"
    assert gateway.calls == []
    assert await _terminal_event_types(db_session, run.id) == []


async def test_concurrent_api_and_recovery_cancel_emit_exactly_one_event(
    monkeypatch,
) -> None:
    """API/引擎取消收口与恢复循环孤儿收口并发：行锁串行化——恰好一个
    run.cancelled 终态事件，后到者幂等成功（不是 IntegrityError）。

    门控 ``_terminal_event_locked`` 让恢复侧在持锁后暂停，强制 API 侧走到
    行锁等待，确定性覆盖"先查后写"竞态窗口。
    """
    from tests.agent_runtime.test_events import _create_committed_run, _purge_committed

    user_id, session_id, run_id = await _create_committed_run()
    # 崩溃残留孤儿：running + 过期租约 + cancel_requested（独立已提交事务）。
    async with SessionFactory() as db:
        repo = AgentRunRepository(db)
        await repo.begin_attempt(run_id)
        assert await repo.claim_lease(run_id, "dead-worker", 300)
        run = await db.get(AgentRun, run_id)
        run.lease_expires_at = utc_now() - timedelta(seconds=10)
        run.cancel_requested = True
        await db.commit()

    a_holds_lock = asyncio.Event()
    release_a = asyncio.Event()
    original = AgentEventStream._terminal_event_locked
    gate = {"armed": True}

    async def gated(self, rid: str):
        # 只门控第一个到达者（恢复侧先启动，必然先持锁）。
        if gate["armed"]:
            gate["armed"] = False
            a_holds_lock.set()
            await release_a.wait()
        return await original(self, rid)

    monkeypatch.setattr(AgentEventStream, "_terminal_event_locked", gated)
    try:

        def engine_factory(db, worker_id, channel_permissions=()):
            raise AssertionError("cancel orphan must not execute engine")

        executor = AgentRunExecutor(
            session_factory=SessionFactory,
            engine_factory=engine_factory,
            worker_id="recovery-worker",
            claim_interval_seconds=0.01,
        )
        task_a = asyncio.create_task(executor.settle_cancel_requested(run_id))
        await asyncio.wait_for(a_holds_lock.wait(), timeout=5)
        async with SessionFactory() as db_b:
            # API/引擎侧并发收口同一 Run（同一 settle_terminal 事务边界）。
            stream_b = AgentEventStream(db_b, AgentEventBroker())
            task_b = asyncio.create_task(
                stream_b.settle_terminal(run_id, user_id, RunStatus.CANCELLED, {})
            )
            await asyncio.sleep(0.3)
            assert not task_b.done()  # B 被 Run 行锁串行化，等待 A 提交
            release_a.set()
            event_b = await task_b
        settled_a = await task_a

        assert settled_a is True
        assert event_b is None  # 后到者幂等，不是唯一键异常

        async with SessionFactory() as db:
            rows = list(
                (
                    await db.scalars(select(AgentEvent).where(AgentEvent.run_id == run_id))
                ).all()
            )
            terminal = [row for row in rows if is_terminal_event(row.event_type)]
            assert [row.event_type for row in terminal] == ["run.cancelled"]
            run = await db.get(AgentRun, run_id)
            assert run.status == RunStatus.CANCELLED
    finally:
        await _purge_committed(user_id, session_id, run_id)
