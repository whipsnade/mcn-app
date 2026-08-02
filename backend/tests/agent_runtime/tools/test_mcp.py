"""Agent MCP 桥测试（设计文档 §11「MCP 故障与积分」/ §10.2「大结果处理」）。

覆盖：
- 四种故障分类的积分状态（definitely_not_sent 释放 / failed_confirmed 释放 /
  result_unknown 保持预留 / settled 结算 10 分）；
- 外发前持久化 + logical_call_id 幂等（相同调用不重复执行或扣费）；
- 成功时 Evidence 保存完整 raw_payload、payload_hash 与受限 preview；
- 仅 MCP 工具计 10 分，计算/历史/Artifact 工具 0 分（§11.3）；
- 细粒度熔断只封锁相同调用，不波及情感工具与不同参数趋势。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Mapping
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.agent_runtime.circuit_breaker import FineGrainedCircuitBreaker
from app.agent_runtime.models import (
    AgentRun,
    AgentRunAttempt,
    AgentSession,
    AgentStep,
    AgentToolCall,
    AgentToolCallReconciliation,
    EvidenceItem,
)
from app.agent_runtime.tools.contracts import ToolContext, ToolResult
from app.agent_runtime.tools.mcp import (
    FAILED_CONFIRMED,
    MCP_POINTS_COST,
    RESULT_UNKNOWN,
    AgentMcpTool,
    logical_call_id_for,
)
from app.agent_runtime.tools.registry import ToolRegistry
from app.billing.service import WalletService
from app.mcp_gateway.contracts import DataTapService
from app.mcp_gateway.transport import (
    McpConnectionTimeout,
    McpGatewayTimeout,
    McpProtocolError,
    McpUpstreamHttpError,
    PossiblySentTimeout,
    RemoteToolResult,
)
from app.mcp_gateway.validation import canonical_json_bytes

INPUT_SCHEMA = {
    "type": "object",
    "properties": {"keyword": {"type": "string"}, "platform": {"type": "string"}},
    "required": ["keyword"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"result": {"type": "string"}},
    "required": ["result"],
    "additionalProperties": False,
}

OK_PAYLOAD = {"result": json.dumps({"rows": [{"keyword": "美妆", "volume": 123}], "total": 1}, ensure_ascii=False)}
ERR_PAYLOAD = {"result": "达人不存在"}
INTERNAL_NAME = "query_analysis_data"
REMOTE_NAME = "datatap.insight.query.analysis.v1"


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _ok_result(upstream_request_id: str = "req-ok") -> RemoteToolResult:
    return RemoteToolResult(
        structured_content=OK_PAYLOAD, is_error=False, upstream_request_id=upstream_request_id
    )


class FakeMcpTransport:
    """记录调用并按序吐出预先编排的 outcome；可查询 DB 中已持久化的行。"""

    def __init__(
        self,
        outcomes: list[Any],
        *,
        db_session=None,
        run_id: str | None = None,
        step_id: str | None = None,
        internal_name: str | None = None,
    ) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[DataTapService, str, dict[str, Any]]] = []
        self.reconciled: dict[str, RemoteToolResult] = {}
        self._db_session = db_session
        self._run_id = run_id
        self._step_id = step_id
        self._internal_name = internal_name
        self.status_at_dispatch: str | None = None

    async def call_tool(self, service: DataTapService, remote_name: str, arguments: Mapping[str, Any]):
        self.calls.append((service, remote_name, dict(arguments)))
        if (
            self._db_session is not None
            and self._run_id
            and self._step_id
            and self._internal_name
        ):
            args_hash = hashlib.sha256(canonical_json_bytes(arguments)).hexdigest()
            logical_id = logical_call_id_for(
                self._run_id, self._step_id, self._internal_name, args_hash
            )
            row = await self._db_session.scalar(
                select(AgentToolCall).where(AgentToolCall.logical_call_id == logical_id)
            )
            self.status_at_dispatch = row.status if row is not None else None
        if not self.outcomes:
            raise AssertionError("unexpected transport dispatch")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if getattr(outcome, "upstream_request_id", None):
            self.reconciled[outcome.upstream_request_id] = outcome
        return outcome

    async def reconcile_tool_call(self, upstream_request_id: str) -> RemoteToolResult | None:
        return self.reconciled.get(upstream_request_id)


async def _make_chain(db_session, user_id: str) -> tuple[AgentSession, AgentRun, AgentStep]:
    now = _now()
    session = AgentSession(
        id=str(uuid4()), user_id=user_id, title="会话", status="active", created_at=now, updated_at=now
    )
    db_session.add(session)
    await db_session.flush()
    run = AgentRun(
        id=str(uuid4()),
        session_id=session.id,
        user_id=user_id,
        profile_name="session_analyst_v1",
        profile_version="v1",
        model="test-model",
        status="running",
    )
    db_session.add(run)
    await db_session.flush()
    attempt = AgentRunAttempt(id=str(uuid4()), run_id=run.id, attempt=1, started_at=now)
    db_session.add(attempt)
    await db_session.flush()
    step = AgentStep(
        id=str(uuid4()),
        run_id=run.id,
        attempt_id=attempt.id,
        sequence=1,
        step_type="tool_call",
        status="running",
        created_at=now,
    )
    db_session.add(step)
    await db_session.flush()
    return session, run, step


def _context(session: AgentSession, run: AgentRun, step: AgentStep, user_id: str) -> ToolContext:
    return ToolContext(
        user_id=user_id,
        session_id=session.id,
        run_id=run.id,
        profile_name="session_analyst_v1",
        step_id=step.id,
    )


def _bridge(
    db_session,
    transport,
    *,
    internal_name: str = INTERNAL_NAME,
    breaker: FineGrainedCircuitBreaker | None = None,
) -> AgentMcpTool:
    return AgentMcpTool(
        internal_name=internal_name,
        service=DataTapService.INSIGHT_CUBE,
        remote_name=REMOTE_NAME,
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        db_session=db_session,
        transport=transport,
        breaker=breaker or FineGrainedCircuitBreaker(failure_threshold=3, reset_seconds=60),
    )


async def _funded_user(db_session, user_factory):
    user = await user_factory()
    await WalletService(db_session).ensure_welcome_grant(user.id)
    return user


async def _only_row(db_session, run_id: str) -> AgentToolCall:
    return await db_session.scalar(
        select(AgentToolCall).where(AgentToolCall.run_id == run_id)
    )


# ---------------------------------------------------------------------------
# 1. 四种故障分类与积分（§11.1）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_definitely_not_sent_releases_reservation(db_session, user_factory) -> None:
    user = await _funded_user(db_session, user_factory)
    session, run, step = await _make_chain(db_session, user.id)
    transport = FakeMcpTransport([McpConnectionTimeout("connect timeout")])
    bridge = _bridge(db_session, transport)

    result = await bridge.execute(_context(session, run, step, user.id), {"keyword": "美妆"})

    assert result.status == "failed"
    assert result.error_type == "definitely_not_sent"
    wallet = await WalletService(db_session).get_wallet(user.id)
    assert wallet.balance == 1000
    assert wallet.reserved == 0
    row = await _only_row(db_session, run.id)
    assert row.status == "failed"
    assert row.points_reserved == 0
    assert row.points_settled == 0
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_failed_confirmed_releases_reservation(db_session, user_factory) -> None:
    user = await _funded_user(db_session, user_factory)
    session, run, step = await _make_chain(db_session, user.id)
    transport = FakeMcpTransport(
        [
            RemoteToolResult(
                structured_content=ERR_PAYLOAD,
                is_error=True,
                upstream_request_id="req-err",
                error_text="达人不存在",
            )
        ]
    )
    bridge = _bridge(db_session, transport)

    result = await bridge.execute(_context(session, run, step, user.id), {"keyword": "美妆"})

    assert result.status == "failed"
    assert result.error_type == "failed_confirmed"
    wallet = await WalletService(db_session).get_wallet(user.id)
    assert wallet.balance == 1000
    assert wallet.reserved == 0
    row = await _only_row(db_session, run.id)
    assert row.status == "failed"


@pytest.mark.asyncio
async def test_result_unknown_keeps_reservation(db_session, user_factory) -> None:
    user = await _funded_user(db_session, user_factory)
    session, run, step = await _make_chain(db_session, user.id)
    transport = FakeMcpTransport([PossiblySentTimeout("read timeout")])
    bridge = _bridge(db_session, transport)

    result = await bridge.execute(_context(session, run, step, user.id), {"keyword": "美妆"})

    assert result.status == "unknown"
    assert result.error_type == "result_unknown"
    # 保持预留：余额已扣、预留挂起
    wallet = await WalletService(db_session).get_wallet(user.id)
    assert wallet.balance == 990
    assert wallet.reserved == 10
    row = await _only_row(db_session, run.id)
    assert row.status == "unknown"
    assert row.points_reserved == 10
    assert row.points_settled == 0


@pytest.mark.asyncio
async def test_settled_charges_10_and_writes_evidence(db_session, user_factory) -> None:
    user = await _funded_user(db_session, user_factory)
    session, run, step = await _make_chain(db_session, user.id)
    transport = FakeMcpTransport([_ok_result()])
    bridge = _bridge(db_session, transport)

    result = await bridge.execute(_context(session, run, step, user.id), {"keyword": "美妆"})

    assert result.status == "success"
    assert result.evidence_id is not None
    wallet = await WalletService(db_session).get_wallet(user.id)
    assert wallet.balance == 990
    assert wallet.reserved == 0
    row = await _only_row(db_session, run.id)
    assert row.status == "settled"
    assert row.points_settled == MCP_POINTS_COST == 10
    # Evidence 保存完整原始 payload + hash + 受限 preview
    evidence = await db_session.get(EvidenceItem, result.evidence_id)
    assert evidence is not None
    assert evidence.raw_payload_json == OK_PAYLOAD
    assert evidence.payload_hash == hashlib.sha256(canonical_json_bytes(OK_PAYLOAD)).hexdigest()
    # 模型可见的 preview 是受限结构化摘要（§10.2），不是完整原始 payload
    assert evidence.normalized_preview_json != OK_PAYLOAD
    preview = evidence.normalized_preview_json
    assert "preview" in preview
    assert "row_count" in preview
    assert "truncated" in preview
    assert "available_fields" in preview
    assert preview["payload_hash"] == evidence.payload_hash


@pytest.mark.asyncio
async def test_504_and_unconfirmable_5xx_keep_reservation(db_session, user_factory) -> None:
    for exc in (McpGatewayTimeout("504"), McpUpstreamHttpError("502"), McpProtocolError("proto")):
        user = await _funded_user(db_session, user_factory)
        session, run, step = await _make_chain(db_session, user.id)
        transport = FakeMcpTransport([exc])
        bridge = _bridge(db_session, transport)

        result = await bridge.execute(_context(session, run, step, user.id), {"keyword": "美妆"})

        assert result.status == "unknown"
        assert result.error_type == "result_unknown"
        wallet = await WalletService(db_session).get_wallet(user.id)
        assert wallet.reserved == 10, exc


# ---------------------------------------------------------------------------
# 2. 外发前持久化与 logical_call_id 幂等（§17.2）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_durable_before_send_and_idempotent(db_session, user_factory) -> None:
    user = await _funded_user(db_session, user_factory)
    session, run, step = await _make_chain(db_session, user.id)
    transport = FakeMcpTransport(
        [_ok_result("req-ok")],
        db_session=db_session,
        run_id=run.id,
        step_id=step.id,
        internal_name=INTERNAL_NAME,
    )
    bridge = _bridge(db_session, transport)
    context = _context(session, run, step, user.id)

    await bridge.execute(context, {"keyword": "美妆"})
    # 外发瞬间行必须已持久化并处于 running（durable-before-send）
    assert transport.status_at_dispatch == "running"
    assert transport.calls[0][1] == REMOTE_NAME

    second = await bridge.execute(context, {"keyword": "美妆"})
    # 相同 logical_call_id 不重复执行或扣费
    assert len(transport.calls) == 1
    assert second.status == "success"
    wallet = await WalletService(db_session).get_wallet(user.id)
    assert wallet.balance == 990
    assert wallet.reserved == 0
    rows = (
        await db_session.scalars(select(AgentToolCall).where(AgentToolCall.run_id == run.id))
    ).all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_crash_after_remote_return_does_not_resend(db_session, user_factory) -> None:
    """进程在远端返回后、Evidence 落库前崩溃：重入必须复用行而不重发。"""
    user = await _funded_user(db_session, user_factory)
    session, run, step = await _make_chain(db_session, user.id)
    args_hash = hashlib.sha256(canonical_json_bytes({"keyword": "美妆"})).hexdigest()
    logical_id = logical_call_id_for(run.id, step.id, INTERNAL_NAME, args_hash)
    now = _now()
    db_session.add(
        AgentToolCall(
            id=str(uuid4()),
            run_id=run.id,
            step_id=step.id,
            logical_call_id=logical_id,
            service=DataTapService.INSIGHT_CUBE.value,
            internal_tool_name=INTERNAL_NAME,
            arguments_json={"keyword": "美妆"},
            arguments_hash=args_hash,
            status="running",
            points_reserved=MCP_POINTS_COST,
            started_at=now,
        )
    )
    await db_session.flush()

    transport = FakeMcpTransport([])  # 任何外发都会失败
    bridge = _bridge(db_session, transport)
    result = await bridge.execute(_context(session, run, step, user.id), {"keyword": "美妆"})

    assert transport.calls == []
    assert result.status == "unknown"
    assert result.error_type == "result_unknown"


# ---------------------------------------------------------------------------
# 3. 细粒度熔断只封锁相同调用（§11.2）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_breaker_blocks_only_same_call_after_failures(db_session, user_factory) -> None:
    """连续让趋势工具失败超过阈值后：相同调用被熔断，情感工具和不同参数
    趋势调用仍到达 fake transport（§11.2）。"""
    user = await _funded_user(db_session, user_factory)
    session, run, step = await _make_chain(db_session, user.id)
    attempt = await db_session.scalar(
        select(AgentRunAttempt).where(AgentRunAttempt.run_id == run.id)
    )
    # 不同 step_id → 不同 logical_call_id：每次都是重新外发，但共享同一熔断键
    #（service + 工具 + 参数哈希）。
    steps = [step]
    for sequence in range(2, 7):
        extra = AgentStep(
            id=str(uuid4()),
            run_id=run.id,
            attempt_id=attempt.id,
            sequence=sequence,
            step_type="tool_call",
            status="running",
            created_at=_now(),
        )
        db_session.add(extra)
        steps.append(extra)
    await db_session.flush()

    breaker = FineGrainedCircuitBreaker(failure_threshold=3, reset_seconds=60)
    transport = FakeMcpTransport(
        [PossiblySentTimeout("t")] * 3
        + [RemoteToolResult(structured_content={"result": "s"}, is_error=False, upstream_request_id="req-sent")]
        + [RemoteToolResult(structured_content={"result": "d"}, is_error=False, upstream_request_id="req-diff")]
    )
    trend = _bridge(db_session, transport, internal_name="social_statistic_trend", breaker=breaker)
    sentiment = _bridge(db_session, transport, internal_name="social_statistic_sentiment", breaker=breaker)

    # 三次相同 service+工具+参数 失败 → 熔断打开
    for index in range(3):
        result = await trend.execute(_context(session, run, steps[index], user.id), {"keyword": "美妆", "platform": "xiaohongshu"})
        assert result.error_type == "result_unknown"
    assert len(transport.calls) == 3

    # 第 4 次相同调用被细粒度熔断拦截，不再外发
    blocked = await trend.execute(_context(session, run, steps[3], user.id), {"keyword": "美妆", "platform": "xiaohongshu"})
    assert blocked.status == "failed"
    assert blocked.error_type == "definitely_not_sent"
    assert len(transport.calls) == 3

    # 情感工具仍到达 fake transport
    sentiment_result = await sentiment.execute(_context(session, run, steps[4], user.id), {"keyword": "美妆"})
    assert sentiment_result.status == "success"
    assert len(transport.calls) == 4

    # 不同参数的趋势调用仍到达 fake transport
    diff = await trend.execute(_context(session, run, steps[5], user.id), {"keyword": "护肤", "platform": "douyin"})
    assert diff.status == "success"
    assert len(transport.calls) == 5


# ---------------------------------------------------------------------------
# 4. 仅 MCP 工具计分（§11.3）
# ---------------------------------------------------------------------------


class FakeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str


class FakeInternalTool:
    def __init__(self, name: str, points_cost: int) -> None:
        self.name = name
        self.input_model = FakeArgs
        self.points_cost = points_cost
        self.external_side_effect = False

    async def execute(self, context: ToolContext, arguments: BaseModel) -> ToolResult:
        return ToolResult(status="success", safe_summary=f"{self.name} ok")


@pytest.mark.asyncio
async def test_only_mcp_tools_cost_points(db_session, user_factory) -> None:
    user = await _funded_user(db_session, user_factory)
    session, run, step = await _make_chain(db_session, user.id)
    bridge = _bridge(db_session, FakeMcpTransport([]))
    # MCP 桥固定 10 分
    assert bridge.points_cost == MCP_POINTS_COST == 10

    # 计算/历史/Artifact 工具 0 分
    registry = ToolRegistry()
    registry.register(FakeInternalTool("calculate_expression", 0), category="calculation")
    registry.register(FakeInternalTool("read_artifact", 0), category="history")
    registry.register(FakeInternalTool("create_artifact", 0), category="artifact")
    costs = {entry.internal_name: entry.points_cost for entry in registry.registered_tools}
    assert costs == {
        "calculate_expression": 0,
        "read_artifact": 0,
        "create_artifact": 0,
    }


# ---------------------------------------------------------------------------
# 5. 恢复核对：按 logical_call_id 只读核对、绝不重放（§11.1 / Step 7）
# ---------------------------------------------------------------------------


async def _make_unknown_call(
    db_session, user_id: str, run: AgentRun, step: AgentStep, *, upstream_request_id: str
) -> tuple[str, AgentToolCall]:
    args_hash = hashlib.sha256(canonical_json_bytes({"keyword": "美妆"})).hexdigest()
    logical_id = logical_call_id_for(run.id, step.id, INTERNAL_NAME, args_hash)
    now = _now()
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


@pytest.mark.asyncio
async def test_reconcile_keeps_unknown_without_replay(db_session, user_factory) -> None:
    user = await _funded_user(db_session, user_factory)
    session, run, step = await _make_chain(db_session, user.id)
    logical_id, call = await _make_unknown_call(db_session, user.id, run, step, upstream_request_id="req-1")
    transport = FakeMcpTransport([])  # 任何外发都会失败
    bridge = _bridge(db_session, transport)

    result = await bridge.reconcile(logical_id)

    assert result.status == "unknown"
    assert result.error_type == RESULT_UNKNOWN
    assert transport.calls == []  # 只读核对，绝不重放
    reconciliation = await db_session.scalar(
        select(AgentToolCallReconciliation).where(
            AgentToolCallReconciliation.tool_call_id == call.id
        )
    )
    assert reconciliation is not None
    assert reconciliation.source == "upstream_probe"
    assert reconciliation.decision == "keep_unknown"


@pytest.mark.asyncio
async def test_reconcile_confirms_failure_and_releases(db_session, user_factory) -> None:
    user = await _funded_user(db_session, user_factory)
    session, run, step = await _make_chain(db_session, user.id)
    logical_id, call = await _make_unknown_call(db_session, user.id, run, step, upstream_request_id="req-1")
    transport = FakeMcpTransport([])
    transport.reconciled["req-1"] = RemoteToolResult(
        structured_content=None, is_error=True, upstream_request_id="req-1", error_text="failed"
    )
    bridge = _bridge(db_session, transport)

    result = await bridge.reconcile(logical_id)

    assert result.status == "failed"
    assert result.error_type == FAILED_CONFIRMED
    assert transport.calls == []
    wallet = await WalletService(db_session).get_wallet(user.id)
    assert wallet.balance == 1000
    assert wallet.reserved == 0
    reconciliation = await db_session.scalar(
        select(AgentToolCallReconciliation).where(
            AgentToolCallReconciliation.tool_call_id == call.id
        )
    )
    assert reconciliation.decision == "confirm_failure"


@pytest.mark.asyncio
async def test_reconcile_confirms_success_settles_and_writes_evidence(
    db_session, user_factory
) -> None:
    user = await _funded_user(db_session, user_factory)
    session, run, step = await _make_chain(db_session, user.id)
    logical_id, call = await _make_unknown_call(db_session, user.id, run, step, upstream_request_id="req-1")
    transport = FakeMcpTransport([])
    transport.reconciled["req-1"] = RemoteToolResult(
        structured_content={"result": "ok"}, is_error=False, upstream_request_id="req-1"
    )
    bridge = _bridge(db_session, transport)

    result = await bridge.reconcile(logical_id)

    assert result.status == "success"
    assert result.evidence_id is not None
    wallet = await WalletService(db_session).get_wallet(user.id)
    assert wallet.balance == 990
    assert wallet.reserved == 0
    evidence = await db_session.get(EvidenceItem, result.evidence_id)
    assert evidence is not None
    assert evidence.tool_call_id == call.id
    reconciliation = await db_session.scalar(
        select(AgentToolCallReconciliation).where(
            AgentToolCallReconciliation.tool_call_id == call.id
        )
    )
    assert reconciliation.decision == "confirm_success"
