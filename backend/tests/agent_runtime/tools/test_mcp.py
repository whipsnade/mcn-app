"""Agent MCP 桥测试（设计文档 §11「MCP 故障与积分」/ §10.2 / v3 加固设计 §5.3）。

真实提交语义：所有用例在独立已提交事务中搭建 user+wallet+session+run+attempt+step
（真实 ``SessionFactory``，用例结束显式清理）。``AgentMcpTool`` 经
``DurableToolCallCoordinator`` 用**独立会话独立事务**提交 prepare/finalize——
外发瞬间调用行与积分预留对**其它连接**已可见（durable-before-send，§2.1）；
崩溃后恢复扫描与只读核对可在独立会话收口，绝不重发、不重复扣费、不错误释放。

覆盖：
- 四种故障分类的积分状态（definitely_not_sent 释放 / failed_confirmed 释放 /
  result_unknown 保持预留 / settled 结算 10 分）；
- durable-before-send：外发瞬间独立会话可见 running 行 + 10 分预留；
- 崩溃恢复：外发后进程崩溃（未 finalize）→ 行保持 running、预留挂起，
  重入同一 logical_call_id 只回放绝不重发；
- 成功 Evidence：完整 raw_payload + hash + 受限 preview；
- 细粒度熔断跨实例共享：同一熔断器下相同调用被封，不同工具/参数不受影响；
- reconcile 四类结果与"取回 payload 必须过输出 Schema 校验，否则不落 Evidence"；
- 仅 MCP 工具计 10 分（§11.3）；余额不足无残留行，充值后可重试。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict, SecretStr
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
    DEFINITELY_NOT_SENT,
    FAILED_CONFIRMED,
    MCP_POINTS_COST,
    RESULT_UNKNOWN,
    AgentMcpTool,
    logical_call_id_for,
)
from app.agent_runtime.tools.registry import ToolRegistry
from app.agent_runtime.evidence import EvidenceWriter
from app.agent_runtime.transcript import RunTranscriptLoader
from app.billing.models import Wallet
from app.billing.service import WalletService
from app.db.session import SessionFactory
from app.identity.models import User
from app.mcp_gateway.contracts import DataTapService
from app.mcp_gateway.datatap import DataTapTransport
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


class _SimulatedCrash(BaseException):
    """模拟进程在外发后、finalize 前崩溃（非 Exception，不被故障分类捕获）。"""


class FakeMcpTransport:
    """记录调用并按序吐出预编排 outcome；可在外发瞬间用独立连接做持久化核对。"""

    def __init__(
        self,
        outcomes: list[Any],
        *,
        dispatch_probe: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[DataTapService, str, dict[str, Any]]] = []
        self.reconciled: dict[str, RemoteToolResult] = {}
        self._dispatch_probe = dispatch_probe

    async def call_tool(self, service: DataTapService, remote_name: str, arguments: Mapping[str, Any]):
        self.calls.append((service, remote_name, dict(arguments)))
        if self._dispatch_probe is not None:
            # 外发瞬间：durable-before-send 要求行 + 预留已提交，独立连接必须可见。
            await self._dispatch_probe()
        if not self.outcomes:
            raise AssertionError("unexpected transport dispatch")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if getattr(outcome, "upstream_request_id", None):
            self.reconciled[outcome.upstream_request_id] = outcome
        return outcome

    async def reconcile_tool_call(self, upstream_request_id: str) -> RemoteToolResult | None:
        return self.reconciled.get(upstream_request_id)


# ---------------------------------------------------------------------------
# 已提交链路搭建 / 清理（真实 SessionFactory，独立连接）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Chain:
    user_id: str
    session_id: str
    run_id: str
    attempt_id: str
    step_ids: tuple[str, ...]

    @property
    def step_id(self) -> str:
        return self.step_ids[0]


async def _setup_chain(*, balance: int = 1000, steps: int = 1) -> _Chain:
    """在独立已提交事务中创建 user+wallet+session+run+attempt+N 个 step。"""
    now = _now()
    async with SessionFactory.begin() as db:
        user = User(
            id=str(uuid4()), nickname="MCP桥测试用户", role="user",
            status="active", created_at=now, updated_at=now,
        )
        db.add(user)
        await db.flush()
        db.add(Wallet(user_id=user.id, balance=balance, reserved=0, version=0, updated_at=now))
        session = AgentSession(
            id=str(uuid4()), user_id=user.id, title="MCP桥会话", status="active",
            created_at=now, updated_at=now,
        )
        db.add(session)
        await db.flush()
        # agent_runs 与 agent_messages 存在环形外键，逐条 flush 保证父行先落库。
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
        step_ids: list[str] = []
        for sequence in range(1, steps + 1):
            step = AgentStep(
                id=str(uuid4()), run_id=run.id, attempt_id=attempt.id, sequence=sequence,
                step_type="tool_call", status="running", visibility="user", created_at=now,
            )
            db.add(step)
            await db.flush()
            step_ids.append(step.id)
        chain = _Chain(
            user_id=user.id,
            session_id=session.id,
            run_id=run.id,
            attempt_id=attempt.id,
            step_ids=tuple(step_ids),
        )
    return chain


async def _teardown_chain(chain: _Chain) -> None:
    """清理已提交的测试链：agent_tool_calls.step_id 无级联，须先删调用子行。"""
    async with SessionFactory() as db:
        call_ids = list(
            (
                await db.scalars(
                    select(AgentToolCall.id).where(AgentToolCall.run_id == chain.run_id)
                )
            ).all()
        )
        for call_id in call_ids:
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
        # 其余（session/run/attempt/step/wallet/流水）由 users 的级联删除收尾。
        user = await db.get(User, chain.user_id)
        if user is not None:
            await db.delete(user)
        await db.commit()


def _context(chain: _Chain, step_id: str | None = None) -> ToolContext:
    return ToolContext(
        user_id=chain.user_id,
        session_id=chain.session_id,
        run_id=chain.run_id,
        profile_name="session_analyst_v1",
        step_id=step_id or chain.step_id,
    )


def _bridge(
    transport: FakeMcpTransport,
    *,
    internal_name: str = INTERNAL_NAME,
    breaker: FineGrainedCircuitBreaker | None = None,
) -> AgentMcpTool:
    # session_factory 缺省为真实 SessionFactory：durable 写入走独立连接真实提交。
    return AgentMcpTool(
        internal_name=internal_name,
        service=DataTapService.INSIGHT_CUBE,
        remote_name=REMOTE_NAME,
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        transport=transport,
        breaker=breaker or FineGrainedCircuitBreaker(failure_threshold=3, reset_seconds=60),
    )


async def _wallet(user_id: str) -> Wallet:
    async with SessionFactory() as db:
        wallet = await db.get(Wallet, user_id)
        assert wallet is not None
        return Wallet(
            user_id=wallet.user_id, balance=wallet.balance, reserved=wallet.reserved,
            version=wallet.version, updated_at=wallet.updated_at,
        )


async def _rows(run_id: str) -> list[AgentToolCall]:
    async with SessionFactory() as db:
        return list(
            (await db.scalars(select(AgentToolCall).where(AgentToolCall.run_id == run_id))).all()
        )


async def _only_row(run_id: str) -> AgentToolCall:
    rows = await _rows(run_id)
    assert len(rows) == 1
    return rows[0]


async def _reconciliation(call_id: str) -> AgentToolCallReconciliation | None:
    async with SessionFactory() as db:
        return await db.scalar(
            select(AgentToolCallReconciliation).where(
                AgentToolCallReconciliation.tool_call_id == call_id
            )
        )


def _transcript_tool_results(messages) -> list[dict]:
    """抽出 transcript 中全部 user 角色 tool_result 负载。"""
    results = []
    for message in messages:
        if message.role != "user":
            continue
        try:
            payload = json.loads(message.content)
        except ValueError:
            continue
        if isinstance(payload, dict) and "tool_result" in payload:
            results.append(payload["tool_result"])
    return results


# ---------------------------------------------------------------------------
# 1. 四种故障分类与积分（§11.1）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_definitely_not_sent_releases_reservation() -> None:
    chain = await _setup_chain()
    try:
        transport = FakeMcpTransport([McpConnectionTimeout("connect timeout")])
        bridge = _bridge(transport)

        result = await bridge.execute(_context(chain), {"keyword": "美妆"})

        assert result.status == "failed"
        assert result.error_type == "definitely_not_sent"
        wallet = await _wallet(chain.user_id)
        assert (wallet.balance, wallet.reserved) == (1000, 0)
        row = await _only_row(chain.run_id)
        assert row.status == "failed"
        assert row.points_reserved == 0
        assert row.points_settled == 0
        assert len(transport.calls) == 1
    finally:
        await _teardown_chain(chain)


@pytest.mark.asyncio
async def test_failed_confirmed_releases_reservation() -> None:
    chain = await _setup_chain()
    try:
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
        bridge = _bridge(transport)

        result = await bridge.execute(_context(chain), {"keyword": "美妆"})

        assert result.status == "failed"
        assert result.error_type == "failed_confirmed"
        # 上游错误文本（脱敏截断后）必须回喂模型，供其修正参数
        assert "达人不存在" in result.safe_summary
        wallet = await _wallet(chain.user_id)
        assert (wallet.balance, wallet.reserved) == (1000, 0)
        row = await _only_row(chain.run_id)
        assert row.status == "failed"
    finally:
        await _teardown_chain(chain)


@pytest.mark.asyncio
async def test_result_unknown_keeps_reservation() -> None:
    chain = await _setup_chain()
    try:
        transport = FakeMcpTransport([PossiblySentTimeout("read timeout")])
        bridge = _bridge(transport)

        result = await bridge.execute(_context(chain), {"keyword": "美妆"})

        assert result.status == "unknown"
        assert result.error_type == "result_unknown"
        # 保持预留：余额已扣、预留挂起
        wallet = await _wallet(chain.user_id)
        assert (wallet.balance, wallet.reserved) == (990, 10)
        row = await _only_row(chain.run_id)
        assert row.status == "unknown"
        assert row.points_reserved == 10
        assert row.points_settled == 0
    finally:
        await _teardown_chain(chain)


@pytest.mark.asyncio
async def test_settled_charges_10_and_writes_evidence() -> None:
    chain = await _setup_chain()
    try:
        transport = FakeMcpTransport([_ok_result()])
        bridge = _bridge(transport)

        result = await bridge.execute(_context(chain), {"keyword": "美妆"})

        assert result.status == "success"
        assert result.evidence_id is not None
        wallet = await _wallet(chain.user_id)
        assert (wallet.balance, wallet.reserved) == (990, 0)
        row = await _only_row(chain.run_id)
        assert row.status == "settled"
        assert row.points_settled == MCP_POINTS_COST == 10
        # Evidence 保存完整原始 payload + hash + 受限 preview
        async with SessionFactory() as db:
            evidence = await db.get(EvidenceItem, result.evidence_id)
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
    finally:
        await _teardown_chain(chain)


@pytest.mark.asyncio
async def test_504_and_unconfirmable_5xx_keep_reservation() -> None:
    for exc in (McpGatewayTimeout("504"), McpUpstreamHttpError("502"), McpProtocolError("proto")):
        chain = await _setup_chain()
        try:
            transport = FakeMcpTransport([exc])
            bridge = _bridge(transport)

            result = await bridge.execute(_context(chain), {"keyword": "美妆"})

            assert result.status == "unknown"
            assert result.error_type == "result_unknown"
            wallet = await _wallet(chain.user_id)
            assert wallet.reserved == 10, exc
        finally:
            await _teardown_chain(chain)


# ---------------------------------------------------------------------------
# 2. durable-before-send 与 logical_call_id 幂等（§2.1 / §5.3 / §17.2）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_durable_before_send_visible_from_independent_session() -> None:
    """外发瞬间：独立连接必须已能看到 running 行 + 10 分预留（提交先于外发）。"""
    chain = await _setup_chain()
    observed: list[tuple[str, int, bool] | None] = []
    observed_wallet: list[tuple[int, int]] = []

    async def probe() -> None:
        async with SessionFactory() as db:
            row = await db.scalar(
                select(AgentToolCall).where(AgentToolCall.run_id == chain.run_id)
            )
            observed.append(
                None
                if row is None
                else (row.status, row.points_reserved, row.started_at is not None)
            )
            wallet = await db.get(Wallet, chain.user_id)
            observed_wallet.append((wallet.balance, wallet.reserved))

    try:
        transport = FakeMcpTransport([_ok_result("req-ok")], dispatch_probe=probe)
        bridge = _bridge(transport)

        await bridge.execute(_context(chain), {"keyword": "美妆"})

        # 外发瞬间行已提交为 running、预留 10 分、started_at 已写（durable-before-send）
        assert observed == [("running", MCP_POINTS_COST, True)]
        assert observed_wallet == [(990, 10)]
        assert transport.calls[0][1] == REMOTE_NAME
    finally:
        await _teardown_chain(chain)


@pytest.mark.asyncio
async def test_same_logical_call_replays_without_redispatch_or_double_charge() -> None:
    """相同 logical_call_id 重入只回放已提交行：不重复执行、不重复扣费。"""
    chain = await _setup_chain()
    try:
        transport = FakeMcpTransport([_ok_result("req-ok")])
        bridge = _bridge(transport)

        first = await bridge.execute(_context(chain), {"keyword": "美妆"})
        second = await bridge.execute(_context(chain), {"keyword": "美妆"})

        assert first.status == second.status == "success"
        assert second.evidence_id == first.evidence_id
        assert len(transport.calls) == 1
        wallet = await _wallet(chain.user_id)
        assert (wallet.balance, wallet.reserved) == (990, 0)
        assert len(await _rows(chain.run_id)) == 1
    finally:
        await _teardown_chain(chain)


@pytest.mark.asyncio
async def test_crash_after_dispatch_keeps_committed_row_and_never_resends() -> None:
    """外发后、finalize 前进程崩溃：行保持已提交 running + 预留挂起；
    重入同一 logical_call_id 只回放"等待恢复"，绝不重发（§2.1 阻断项）。"""
    chain = await _setup_chain()
    try:
        transport = FakeMcpTransport([_SimulatedCrash("process died")])
        bridge = _bridge(transport)

        with pytest.raises(_SimulatedCrash):
            await bridge.execute(_context(chain), {"keyword": "美妆"})

        # 崩溃后：调用行与预留对独立会话可见（不再整体回滚）
        row = await _only_row(chain.run_id)
        assert row.status == "running"
        assert row.points_reserved == MCP_POINTS_COST
        wallet = await _wallet(chain.user_id)
        assert (wallet.balance, wallet.reserved) == (990, 10)

        # 模型/引擎用新 Attempt 重发同一调用：prepare 命中已提交行，只回放不重发
        replay = await bridge.execute(_context(chain), {"keyword": "美妆"})
        assert replay.status == "unknown"
        assert replay.error_type == RESULT_UNKNOWN
        assert len(transport.calls) == 1
        # 回放不产生任何钱包变动（不重复扣费、不错误释放）
        wallet = await _wallet(chain.user_id)
        assert (wallet.balance, wallet.reserved) == (990, 10)
    finally:
        await _teardown_chain(chain)


@pytest.mark.asyncio
async def test_cancellation_after_dispatch_closes_as_result_unknown() -> None:
    """进程取消且请求可能已发送：按 result_unknown 收口（保留预留），取消继续抛出。"""
    chain = await _setup_chain()
    dispatched = asyncio.Event()

    class HangingTransport(FakeMcpTransport):
        async def call_tool(self, service, remote_name, arguments):
            self.calls.append((service, remote_name, dict(arguments)))
            dispatched.set()
            await asyncio.Event().wait()  # 永不返回：直到任务被取消
            raise AssertionError("unreachable")

    try:
        transport = HangingTransport([])
        bridge = _bridge(transport)
        task = asyncio.create_task(bridge.execute(_context(chain), {"keyword": "美妆"}))
        await asyncio.wait_for(dispatched.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # 已提交行按 result_unknown 收口：预留保留，等待恢复/人工核对
        row = await _only_row(chain.run_id)
        assert row.status == "unknown"
        assert row.error_type == RESULT_UNKNOWN
        wallet = await _wallet(chain.user_id)
        assert (wallet.balance, wallet.reserved) == (990, 10)
    finally:
        await _teardown_chain(chain)


# ---------------------------------------------------------------------------
# 3. 细粒度熔断（§11.2 / §5.3：跨实例共享同一熔断器才生效）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_breaker_shared_across_tool_instances_blocks_same_call() -> None:
    """跨 AgentMcpTool 实例共享熔断器：同 Run 同参数只外发一次（防重），第 2 次 replay。"""
    chain = await _setup_chain(steps=4)
    try:
        breaker = FineGrainedCircuitBreaker(failure_threshold=3, reset_seconds=60)
        transport = FakeMcpTransport([PossiblySentTimeout("t")])
        bridge_a = _bridge(transport, breaker=breaker)
        bridge_b = _bridge(FakeMcpTransport([]), breaker=breaker)

        # 首次外发失败 → result_unknown；同 Run 同参数后续跨 Step 防重 replay
        result = await bridge_a.execute(
            _context(chain, chain.step_ids[0]), {"keyword": "美妆"}
        )
        assert result.error_type == "result_unknown"
        assert len(transport.calls) == 1

        # 第 4 次（另一实例、另一 step）同参数 → prepare replay（unknown 行），不外发
        blocked = await bridge_b.execute(
            _context(chain, chain.step_ids[3]), {"keyword": "美妆"}
        )
        assert blocked.status == "unknown"
        assert blocked.error_type == "result_unknown"
        assert len(transport.calls) == 1
        wallet = await _wallet(chain.user_id)
        assert (wallet.balance, wallet.reserved) == (990, 10)
    finally:
        await _teardown_chain(chain)


@pytest.mark.asyncio
async def test_breaker_blocks_only_same_call_after_failures() -> None:
    """熔断键 = service+工具+参数哈希：情感工具与不同参数的趋势调用不受影响。"""
    chain = await _setup_chain(steps=6)
    try:
        breaker = FineGrainedCircuitBreaker(failure_threshold=3, reset_seconds=60)
        transport = FakeMcpTransport(
            [PossiblySentTimeout("t")]
            + [_ok_result()]
            + [RemoteToolResult(structured_content={"result": "d"}, is_error=False, upstream_request_id="req-diff")]
        )
        trend = _bridge(transport, internal_name="social_statistic_trend", breaker=breaker)
        sentiment = _bridge(transport, internal_name="social_statistic_sentiment", breaker=breaker)

        for index in range(3):
            result = await trend.execute(
                _context(chain, chain.step_ids[index]),
                {"keyword": "美妆", "platform": "xiaohongshu"},
            )
            assert result.error_type == "result_unknown"
        assert len(transport.calls) == 1

        blocked = await trend.execute(
            _context(chain, chain.step_ids[3]),
            {"keyword": "美妆", "platform": "xiaohongshu"},
        )
        # 同参数跨 Step 防重：prepare 幂等回放已有 unknown 行，不外发
        assert blocked.error_type == "result_unknown"
        assert len(transport.calls) == 1

        sentiment_result = await sentiment.execute(
            _context(chain, chain.step_ids[4]), {"keyword": "美妆"}
        )
        assert sentiment_result.status == "success"
        assert len(transport.calls) == 2

        diff = await trend.execute(
            _context(chain, chain.step_ids[5]), {"keyword": "护肤", "platform": "douyin"}
        )
        assert diff.status == "success"
        assert len(transport.calls) == 3
    finally:
        await _teardown_chain(chain)


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
async def test_only_mcp_tools_cost_points() -> None:
    bridge = _bridge(FakeMcpTransport([]))
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
# 5. 恢复核对：按 logical_call_id 只读核对、绝不重放（§11.1 / §5.3）
# ---------------------------------------------------------------------------


async def _make_unknown_call(
    chain: _Chain, *, upstream_request_id: str | None
) -> tuple[str, str]:
    """已提交事务中创建 unknown 调用 + 10 分预留；返回 (logical_call_id, call_id)。"""
    args_hash = hashlib.sha256(canonical_json_bytes({"keyword": "美妆"})).hexdigest()
    logical_id = logical_call_id_for(chain.run_id, INTERNAL_NAME, args_hash)
    now = _now()
    async with SessionFactory.begin() as db:
        call = AgentToolCall(
            id=str(uuid4()),
            run_id=chain.run_id,
            step_id=chain.step_id,
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
        db.add(call)
        await db.flush()
        await WalletService(db).reserve(
            chain.user_id, MCP_POINTS_COST, f"agent-mcp:{logical_id}:reserve", call.id,
            reference_type="agent_tool_call",
            tenant_source=False,
        )
        call_id = call.id
    return logical_id, call_id


@pytest.mark.asyncio
async def test_reconcile_keeps_unknown_without_replay() -> None:
    chain = await _setup_chain()
    try:
        logical_id, call_id = await _make_unknown_call(chain, upstream_request_id="req-1")
        transport = FakeMcpTransport([])  # reconcile 无缓存结果 → 无法核对
        bridge = _bridge(transport)

        result = await bridge.reconcile(logical_id)

        assert result.status == "unknown"
        assert result.error_type == RESULT_UNKNOWN
        assert transport.calls == []  # 只读核对，绝不重放
        reconciliation = await _reconciliation(call_id)
        assert reconciliation is not None
        assert reconciliation.source == "upstream_probe"
        assert reconciliation.decision == "keep_unknown"
        # 保持预留，绝不超时自动释放
        wallet = await _wallet(chain.user_id)
        assert (wallet.balance, wallet.reserved) == (990, 10)
    finally:
        await _teardown_chain(chain)


@pytest.mark.asyncio
async def test_reconcile_confirms_failure_and_releases() -> None:
    chain = await _setup_chain()
    try:
        logical_id, call_id = await _make_unknown_call(chain, upstream_request_id="req-1")
        transport = FakeMcpTransport([])
        transport.reconciled["req-1"] = RemoteToolResult(
            structured_content=None, is_error=True, upstream_request_id="req-1", error_text="failed"
        )
        bridge = _bridge(transport)

        result = await bridge.reconcile(logical_id)

        assert result.status == "failed"
        assert result.error_type == FAILED_CONFIRMED
        assert transport.calls == []
        wallet = await _wallet(chain.user_id)
        assert (wallet.balance, wallet.reserved) == (1000, 0)
        reconciliation = await _reconciliation(call_id)
        assert reconciliation.decision == "confirm_failure"
    finally:
        await _teardown_chain(chain)


@pytest.mark.asyncio
async def test_reconcile_confirms_success_settles_and_writes_evidence() -> None:
    chain = await _setup_chain()
    try:
        logical_id, call_id = await _make_unknown_call(chain, upstream_request_id="req-1")
        transport = FakeMcpTransport([])
        transport.reconciled["req-1"] = RemoteToolResult(
            structured_content={"result": "ok"}, is_error=False, upstream_request_id="req-1"
        )
        bridge = _bridge(transport)

        result = await bridge.reconcile(logical_id)

        assert result.status == "success"
        assert result.evidence_id is not None
        wallet = await _wallet(chain.user_id)
        assert (wallet.balance, wallet.reserved) == (990, 0)
        async with SessionFactory() as db:
            evidence = await db.get(EvidenceItem, result.evidence_id)
        assert evidence is not None
        assert evidence.tool_call_id == call_id
        assert evidence.raw_payload_json == {"result": "ok"}
        reconciliation = await _reconciliation(call_id)
        assert reconciliation.decision == "confirm_success"
    finally:
        await _teardown_chain(chain)


@pytest.mark.asyncio
async def test_reconcile_confirms_success_without_payload_settles_result_unavailable() -> None:
    chain = await _setup_chain()
    try:
        logical_id, call_id = await _make_unknown_call(chain, upstream_request_id="req-1")
        transport = FakeMcpTransport([])
        transport.reconciled["req-1"] = RemoteToolResult(
            structured_content=None, is_error=False, upstream_request_id="req-1"
        )
        bridge = _bridge(transport)

        result = await bridge.reconcile(logical_id)

        assert result.status == "success"
        assert result.evidence_id is None
        wallet = await _wallet(chain.user_id)
        assert (wallet.balance, wallet.reserved) == (990, 0)
        async with SessionFactory() as db:
            call = await db.get(AgentToolCall, call_id)
            assert call.status == "settled"
            assert call.safe_error_message == "result_unavailable"
            assert (
                await db.scalar(select(EvidenceItem).where(EvidenceItem.tool_call_id == call_id))
            ) is None
    finally:
        await _teardown_chain(chain)


@pytest.mark.asyncio
async def test_reconcile_payload_failing_output_validation_does_not_write_evidence() -> None:
    """reconcile 取回的 payload 必须重新过输出 Schema 校验（§5.3）：
    校验不过 → 不落 Evidence，按 failed_confirmed 释放（与 execute 路径一致）。"""
    chain = await _setup_chain()
    try:
        logical_id, call_id = await _make_unknown_call(chain, upstream_request_id="req-1")
        transport = FakeMcpTransport([])
        transport.reconciled["req-1"] = RemoteToolResult(
            structured_content={"wrong_shape": 123}, is_error=False, upstream_request_id="req-1"
        )
        bridge = _bridge(transport)

        result = await bridge.reconcile(logical_id)

        assert result.status == "failed"
        assert result.error_type == FAILED_CONFIRMED
        assert result.evidence_id is None
        wallet = await _wallet(chain.user_id)
        assert (wallet.balance, wallet.reserved) == (1000, 0)
        async with SessionFactory() as db:
            assert (
                await db.scalar(select(EvidenceItem).where(EvidenceItem.tool_call_id == call_id))
            ) is None
            call = await db.get(AgentToolCall, call_id)
            assert call.status == "failed"
        reconciliation = await _reconciliation(call_id)
        assert reconciliation.decision == "confirm_failure"
        assert "output validation" in (reconciliation.note or "")
    finally:
        await _teardown_chain(chain)


# ---------------------------------------------------------------------------
# 6. 外发前失败也记录到细粒度熔断键（Fix 1）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connection_errors_trip_fine_grained_breaker() -> None:
    """外发前错误（connection）计入细粒度熔断键；definitely_not_sent 允许一次重试。"""
    chain = await _setup_chain(steps=4)
    try:
        breaker = FineGrainedCircuitBreaker(failure_threshold=3, reset_seconds=60)
        transport = FakeMcpTransport([McpConnectionTimeout("connect timeout")] * 3)
        bridge = _bridge(transport, breaker=breaker)

        # 首次外发前失败
        result = await bridge.execute(
            _context(chain, chain.step_ids[0]), {"keyword": "美妆"}
        )
        assert result.error_type == "definitely_not_sent"
        assert len(transport.calls) == 1

        # 第二次同参数：definitely_not_sent 允许重试一次 → 真实外发
        retry = await bridge.execute(
            _context(chain, chain.step_ids[1]), {"keyword": "美妆"}
        )
        assert retry.error_type == "definitely_not_sent"
        assert len(transport.calls) == 2

        # 第三次同参数：已达上限（dispatch_count=2）→ 防重阻止，不外发
        blocked = await bridge.execute(
            _context(chain, chain.step_ids[3]), {"keyword": "美妆"}
        )
        assert blocked.error_type == "definitely_not_sent"
        assert len(transport.calls) == 2
        # 两次失败都释放积分，无挂起
        wallet = await _wallet(chain.user_id)
        assert (wallet.balance, wallet.reserved) == (1000, 0)
    finally:
        await _teardown_chain(chain)


@pytest.mark.asyncio
async def test_half_open_probe_connection_error_does_not_wedge_key() -> None:
    """半开探测以外发前错误失败后，键重新打开而非永久卡死（Fix 1 (b)）。

    跨 Run 同参数才有不同 logical_call_id（新防重模型下同 Run 同参数只外发
    一次），熔断器在跨 Run 场景下计数。
    """
    now = [100.0]
    chains = [await _setup_chain() for _ in range(6)]
    try:
        breaker = FineGrainedCircuitBreaker(
            failure_threshold=3, reset_seconds=30.0, clock=lambda: now[0]
        )
        transport = FakeMcpTransport(
            [McpConnectionTimeout("connect timeout")] * 4 + [_ok_result()]
        )
        bridge = _bridge(transport, breaker=breaker)

        for i in range(3):
            result = await bridge.execute(_context(chains[i]), {"keyword": "美妆"})
            assert result.error_type == "definitely_not_sent"
        assert len(transport.calls) == 3

        now[0] += 40.0
        probe = await bridge.execute(_context(chains[3]), {"keyword": "美妆"})
        assert probe.error_type == "definitely_not_sent"
        assert len(transport.calls) == 4

        assert (
            breaker.allow(DataTapService.INSIGHT_CUBE.value, INTERNAL_NAME, {"keyword": "美妆"})
            is False
        )
        blocked = await bridge.execute(_context(chains[4]), {"keyword": "美妆"})
        assert blocked.error_type == "definitely_not_sent"
        assert len(transport.calls) == 4

        now[0] += 40.0
        ok = await bridge.execute(_context(chains[5]), {"keyword": "美妆"})
        assert ok.status == "success"
        assert len(transport.calls) == 5
    finally:
        for c in chains:
            await _teardown_chain(c)


# ---------------------------------------------------------------------------
# 7. 余额不足：无残留行，充值后可重试（Fix 4）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insufficient_balance_leaves_no_dangling_row_and_retry_proceeds() -> None:
    chain = await _setup_chain(balance=0)
    try:
        transport = FakeMcpTransport([_ok_result()])
        bridge = _bridge(transport)

        result = await bridge.execute(_context(chain), {"keyword": "美妆"})
        assert result.status == "failed"
        assert result.error_type == "definitely_not_sent"
        # 预留与调用行同一事务：余额不足整体回滚，独立会话确认零残留、未外发
        assert await _rows(chain.run_id) == []
        assert transport.calls == []

        # 充值后同一 logical_call_id 可重试并成功
        async with SessionFactory.begin() as db:
            wallet = await db.get(Wallet, chain.user_id)
            wallet.balance = 100
        retry = await bridge.execute(_context(chain), {"keyword": "美妆"})
        assert retry.status == "success"
        assert len(transport.calls) == 1
        row = await _only_row(chain.run_id)
        assert row.status == "settled"
    finally:
        await _teardown_chain(chain)


# ---------------------------------------------------------------------------
# 8. 墙钟超时：真实传输挂起 → result_unknown 收口（cutover 阻断项 1 / UAT Incident #8）
# ---------------------------------------------------------------------------


class _HangingProtocolSession:
    """持续 trickle 的 DataTap 统计查询：永不返回结果，直到任务被取消。"""

    call_count = 0

    def __init__(self, read_stream, write_stream, **_kwargs) -> None:
        self.service = read_stream

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def initialize(self) -> None:
        return None

    async def call_tool(self, _name, _arguments):
        type(self).call_count += 1
        await asyncio.Event().wait()  # read timeout 被 trickle 不断重置，永不触发
        raise AssertionError("unreachable")


def _hanging_datatap_transport() -> DataTapTransport:
    """与生产 Agent 传输同策略（circuit_scope=none / 不重试）+ 小墙钟的真实传输。"""

    @asynccontextmanager
    async def opener(url: str, **_kwargs):
        service = next(item for item in DataTapService if item.value in url)
        yield service, object(), lambda: "session-1"

    return DataTapTransport(
        token=SecretStr("unit-test-token"),
        session_opener=opener,
        session_factory=_HangingProtocolSession,
        circuit_scope="none",
        retry_policy="never",
        call_timeout_seconds=0.3,
        cancel_grace_seconds=0.3,
    )


@pytest.mark.asyncio
async def test_wall_clock_timeout_closes_result_unknown_and_keeps_reservation() -> None:
    """挂起调用在墙钟上限内按 result_unknown 收口：保留预留、Run 不被挂死。"""
    chain = await _setup_chain()
    try:
        _HangingProtocolSession.call_count = 0
        transport = _hanging_datatap_transport()
        bridge = _bridge(transport)

        started = time.monotonic()
        result = await bridge.execute(_context(chain), {"keyword": "美妆"})
        elapsed = time.monotonic() - started

        assert result.status == "unknown"
        assert result.error_type == RESULT_UNKNOWN
        assert elapsed < 3.0  # 受控窗口内收口，而不是挂起数十分钟
        assert _HangingProtocolSession.call_count == 1

        row = await _only_row(chain.run_id)
        assert row.status == "unknown"
        assert row.error_type == RESULT_UNKNOWN
        assert row.points_reserved == MCP_POINTS_COST
        assert row.points_settled == 0
        wallet = await _wallet(chain.user_id)
        assert (wallet.balance, wallet.reserved) == (990, 10)
    finally:
        await _teardown_chain(chain)


@pytest.mark.asyncio
async def test_wall_clock_timeout_counts_toward_fine_grained_breaker() -> None:
    """超时计细粒度熔断失败：跨 Run 同参数反复超时后相同调用被熔断、不再外发。"""
    chains = [await _setup_chain() for _ in range(4)]
    try:
        _HangingProtocolSession.call_count = 0
        transport = _hanging_datatap_transport()
        breaker = FineGrainedCircuitBreaker(failure_threshold=3, reset_seconds=60)
        bridge = _bridge(transport, breaker=breaker)

        for i in range(3):
            result = await bridge.execute(_context(chains[i]), {"keyword": "美妆"})
            assert result.error_type == RESULT_UNKNOWN
        assert _HangingProtocolSession.call_count == 3

        blocked = await bridge.execute(_context(chains[3]), {"keyword": "美妆"})
        assert blocked.status == "failed"
        assert blocked.error_type == "definitely_not_sent"
        assert _HangingProtocolSession.call_count == 3
        wallet = await _wallet(chains[3].user_id)
        assert (wallet.balance, wallet.reserved) == (1000, 0)
    finally:
        for c in chains:
            await _teardown_chain(c)


@pytest.mark.asyncio
async def test_wall_clock_timeout_unknown_reconciles_and_settles_or_releases_once() -> None:
    """超时收口的 unknown：恢复核对只读不重放；确认成功结算 / 确认失败释放，均幂等。"""
    chain = await _setup_chain(steps=2)
    try:
        _HangingProtocolSession.call_count = 0
        transport = _hanging_datatap_transport()
        bridge = _bridge(transport)

        # 调用 1：墙钟超时 → unknown（无 upstream_request_id）
        result = await bridge.execute(
            _context(chain, chain.step_ids[0]), {"keyword": "美妆"}
        )
        assert result.error_type == RESULT_UNKNOWN
        first = (await _rows(chain.run_id))[0]

        # 恢复核对：只读探测，绝不重放；无 upstream_request_id → keep_unknown + 审计
        probe = await bridge.reconcile(first.logical_call_id)
        assert probe.status == "unknown"
        assert _HangingProtocolSession.call_count == 1
        reconciliation = await _reconciliation(first.id)
        assert reconciliation is not None
        assert reconciliation.decision == "keep_unknown"
        wallet = await _wallet(chain.user_id)
        assert (wallet.balance, wallet.reserved) == (990, 10)

        # 上游稍后确认（管理员/恢复核对原语）：结算 10 分 + 补写 Evidence
        snapshot = await bridge._coordinator.load_call(first.logical_call_id)
        assert snapshot is not None
        evidence_id = await bridge._coordinator.confirm_success(
            snapshot,
            validated_payload=OK_PAYLOAD,
            upstream_request_id="req-late",
            note="late outcome confirmed",
        )
        assert evidence_id is not None
        wallet = await _wallet(chain.user_id)
        assert (wallet.balance, wallet.reserved) == (990, 0)

        # 幂等：再次确认成功不重复扣费；重入同一 logical_call_id 只回放不重发
        again = await bridge._coordinator.confirm_success(
            snapshot,
            validated_payload=OK_PAYLOAD,
            upstream_request_id="req-late",
            note="duplicate confirm",
        )
        assert again == evidence_id
        wallet = await _wallet(chain.user_id)
        assert (wallet.balance, wallet.reserved) == (990, 0)
        replay = await bridge.execute(
            _context(chain, chain.step_ids[0]), {"keyword": "美妆"}
        )
        assert replay.status == "success"
        assert _HangingProtocolSession.call_count == 1

        # 调用 2（不同参数，避免防重 replay 已 settled 行）：同样超时收口
        second = await bridge.execute(
            _context(chain, chain.step_ids[1]), {"keyword": "护肤"}
        )
        assert second.error_type == RESULT_UNKNOWN
        wallet = await _wallet(chain.user_id)
        assert (wallet.balance, wallet.reserved) == (980, 10)
        second_row = [
            row for row in await _rows(chain.run_id) if row.id != first.id
        ][0]
        snapshot2 = await bridge._coordinator.load_call(second_row.logical_call_id)
        assert snapshot2 is not None
        await bridge._coordinator.confirm_failure(
            snapshot2, message="upstream confirmed failure", note="late failure confirmed"
        )
        wallet = await _wallet(chain.user_id)
        assert (wallet.balance, wallet.reserved) == (990, 0)
        final = [row for row in await _rows(chain.run_id) if row.id == second_row.id][0]
        assert final.status == "failed"
        assert final.error_type == FAILED_CONFIRMED
    finally:
        await _teardown_chain(chain)


# ---------------------------------------------------------------------------
# 结构化失败反馈矩阵（Gate B Task 6）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error_type", "retry_allowed", "points_state", "transport_outcome"),
    [
        (
            "definitely_not_sent",
            True,
            "released",
            McpConnectionTimeout("connect timeout"),
        ),
        (
            "failed_confirmed",
            False,
            "released",
            RemoteToolResult(
                structured_content=ERR_PAYLOAD,
                is_error=True,
                upstream_request_id="req-err-matrix",
            ),
        ),
        (
            "result_unknown",
            False,
            "reserved",
            McpGatewayTimeout("gateway timeout"),
        ),
        (
            "succeeded_empty",
            False,
            "settled",
            RemoteToolResult(
                structured_content=None,
                is_error=False,
                upstream_request_id="req-empty-matrix",
            ),
        ),
    ],
)
@pytest.mark.asyncio
async def test_feedback_matrix(
    error_type: str, retry_allowed: bool, points_state: str, transport_outcome: Any
) -> None:
    """五类结果矩阵：反馈携带同指纹重试许可与积分状态（Gate B Task 6）。"""
    chain = await _setup_chain()
    try:
        transport = FakeMcpTransport([transport_outcome])
        bridge = _bridge(transport)

        result = await bridge.execute(_context(chain), {"keyword": "美妆"})
        feedback = json.loads(result.safe_summary)
        assert feedback["error_type"] == error_type
        assert feedback["same_fingerprint_retry_allowed"] is retry_allowed
        assert feedback["points_state"] == points_state
        assert feedback["tool"] == INTERNAL_NAME
        assert isinstance(feedback["suggested_actions"], list)
        assert feedback["suggested_actions"]
    finally:
        await _teardown_chain(chain)


@pytest.mark.asyncio
async def test_same_fingerprint_retry_is_rejected_on_replay() -> None:
    """definitely_not_sent 后同参数跨 Step：允许一次重试，第二次失败后阻止。"""
    chain = await _setup_chain(steps=3)
    try:
        transport = FakeMcpTransport([
            McpConnectionTimeout("connect timeout 1"),
            McpConnectionTimeout("connect timeout 2"),
        ])
        bridge = _bridge(transport)

        first = await bridge.execute(_context(chain, chain.step_ids[0]), {"keyword": "美妆"})
        assert json.loads(first.safe_summary)["same_fingerprint_retry_allowed"] is True

        second = await bridge.execute(_context(chain, chain.step_ids[1]), {"keyword": "美妆"})
        assert second.error_type == DEFINITELY_NOT_SENT
        feedback = json.loads(second.safe_summary)
        assert feedback["same_fingerprint_retry_allowed"] is False
        assert len(transport.calls) == 2

        third = await bridge.execute(_context(chain, chain.step_ids[2]), {"keyword": "美妆"})
        assert json.loads(third.safe_summary)["same_fingerprint_retry_allowed"] is False
        assert len(transport.calls) == 2
        wallet = await _wallet(chain.user_id)
        assert (wallet.balance, wallet.reserved) == (1000, 0)
    finally:
        await _teardown_chain(chain)


# ---------------------------------------------------------------------------
# M1: MCP fingerprint 状态机（Gate B 最终审核：跨 Step 防重 + definitely_not_sent 重试）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_m1_unknown_cross_step_only_dispatched_once() -> None:
    """result_unknown 跨 Step 只外发一次，第二次回放 unknown 状态。"""
    chain = await _setup_chain(steps=3)
    try:
        transport = FakeMcpTransport([McpGatewayTimeout("gw timeout")])
        bridge = _bridge(transport)

        # Step 1: 外发 → result_unknown
        r1 = await bridge.execute(_context(chain, chain.step_ids[0]), {"keyword": "美妆"})
        assert r1.error_type == RESULT_UNKNOWN
        assert len(transport.calls) == 1

        # Step 2: 同参数不同 Step → 防重回放，不外发
        r2 = await bridge.execute(_context(chain, chain.step_ids[1]), {"keyword": "美妆"})
        assert r2.error_type == RESULT_UNKNOWN
        assert len(transport.calls) == 1
        # 回放的 feedback 标记 same_fingerprint_retry_allowed=False
        fb2 = json.loads(r2.safe_summary)
        assert fb2["same_fingerprint_retry_allowed"] is False

        # 钱包：一次 unknown 挂 10 预留
        wallet = await _wallet(chain.user_id)
        assert (wallet.balance, wallet.reserved) == (990, 10)
    finally:
        await _teardown_chain(chain)


@pytest.mark.asyncio
async def test_m1_failed_confirmed_cross_step_only_dispatched_once() -> None:
    """failed_confirmed 跨 Step 只外发一次，第二次回放结构化失败。"""
    chain = await _setup_chain(steps=3)
    try:
        transport = FakeMcpTransport([
            RemoteToolResult(structured_content=ERR_PAYLOAD, is_error=True, upstream_request_id="req-err"),
        ])
        bridge = _bridge(transport)

        r1 = await bridge.execute(_context(chain, chain.step_ids[0]), {"keyword": "美妆"})
        assert r1.error_type == FAILED_CONFIRMED
        assert len(transport.calls) == 1

        r2 = await bridge.execute(_context(chain, chain.step_ids[1]), {"keyword": "美妆"})
        assert r2.error_type == FAILED_CONFIRMED
        assert len(transport.calls) == 1
        fb2 = json.loads(r2.safe_summary)
        assert fb2["same_fingerprint_retry_allowed"] is False

        # failed_confirmed 释放积分
        wallet = await _wallet(chain.user_id)
        assert (wallet.balance, wallet.reserved) == (1000, 0)
    finally:
        await _teardown_chain(chain)


@pytest.mark.asyncio
async def test_m1_definitely_not_sent_allows_one_retry() -> None:
    """definitely_not_sent 第一次失败后允许真实重试一次，第三次阻止。总外发=2。"""
    chain = await _setup_chain(steps=4)
    try:
        transport = FakeMcpTransport([
            McpConnectionTimeout("connect timeout 1"),
            McpConnectionTimeout("connect timeout 2"),
        ])
        bridge = _bridge(transport)

        # Step 1: 首次外发 → connection error → definitely_not_sent
        r1 = await bridge.execute(_context(chain, chain.step_ids[0]), {"keyword": "美妆"})
        assert r1.error_type == DEFINITELY_NOT_SENT
        fb1 = json.loads(r1.safe_summary)
        assert fb1["same_fingerprint_retry_allowed"] is True
        assert len(transport.calls) == 1

        # Step 2: 同参数不同 Step → 允许重试（第二次真实外发）
        r2 = await bridge.execute(_context(chain, chain.step_ids[1]), {"keyword": "美妆"})
        assert r2.error_type == DEFINITELY_NOT_SENT
        assert len(transport.calls) == 2

        # Step 3: 同参数 → 第三次阻止（已达上限 2）
        r3 = await bridge.execute(_context(chain, chain.step_ids[2]), {"keyword": "美妆"})
        assert r3.error_type == DEFINITELY_NOT_SENT
        assert len(transport.calls) == 2
        fb3 = json.loads(r3.safe_summary)
        assert fb3["same_fingerprint_retry_allowed"] is False

        # 两次失败都释放积分，无残留
        wallet = await _wallet(chain.user_id)
        assert (wallet.balance, wallet.reserved) == (1000, 0)
    finally:
        await _teardown_chain(chain)


@pytest.mark.asyncio
async def test_m1_succeeded_empty_no_evidence_and_consistent_replay() -> None:
    """succeeded_empty 不创建 Evidence，跨 Step 回放一致返回 failed+succeeded_empty。"""
    chain = await _setup_chain(steps=3)
    try:
        transport = FakeMcpTransport([
            RemoteToolResult(structured_content=None, is_error=False, upstream_request_id="req-empty"),
        ])
        bridge = _bridge(transport)

        # Step 1: 成功但无结构化内容 → succeeded_empty
        r1 = await bridge.execute(_context(chain, chain.step_ids[0]), {"keyword": "美妆"})
        assert r1.status == "failed"
        assert r1.error_type == "succeeded_empty"
        assert len(transport.calls) == 1

        # 不应创建 Evidence
        rows = await _rows(chain.run_id)
        assert len(rows) == 1
        row = rows[0]
        assert row.status == "failed"
        assert row.error_type == "succeeded_empty"
        # 无 Evidence 行
        async with SessionFactory() as db:
            evidence = await EvidenceWriter(db).get_by_tool_call_id(row.id)
            assert evidence is None

        # Step 2: 同参数不同 Step → 回放 succeeded_empty（不是 success/already settled）
        r2 = await bridge.execute(_context(chain, chain.step_ids[1]), {"keyword": "美妆"})
        assert r2.status == "failed"
        assert r2.error_type == "succeeded_empty"
        assert len(transport.calls) == 1

        # 钱包：succeeded_empty 结算（非释放）
        wallet = await _wallet(chain.user_id)
        assert (wallet.balance, wallet.reserved) == (990, 0)
    finally:
        await _teardown_chain(chain)


# ---------------------------------------------------------------------------
# P0: dispatch retry 积分幂等状态机（Gate B：账务幂等键按 dispatch attempt 区分）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p0_dnr_retry_success_settles_once_and_creates_evidence() -> None:
    """DNR → retry success：两次真实派发，最终钱包 available=初始-10、reserved=0，生成 Evidence。"""
    chain = await _setup_chain(steps=3)
    try:
        transport = FakeMcpTransport([
            McpConnectionTimeout("connect timeout"),
            _ok_result(),
        ])
        bridge = _bridge(transport)

        # 第一次派发：connection 错误 → definitely_not_sent
        r1 = await bridge.execute(_context(chain, chain.step_ids[0]), {"keyword": "美妆"})
        assert r1.error_type == DEFINITELY_NOT_SENT
        w1 = await _wallet(chain.user_id)
        assert (w1.balance, w1.reserved) == (1000, 0)

        # 第二次派发（重试）：成功 → 结算
        r2 = await bridge.execute(_context(chain, chain.step_ids[1]), {"keyword": "美妆"})
        assert r2.status == "success"
        assert len(transport.calls) == 2

        # 钱包：available=990（扣 10）、reserved=0
        w2 = await _wallet(chain.user_id)
        assert (w2.balance, w2.reserved) == (990, 0)

        # 调用行：settled，points_reserved=0，points_settled=10
        row = await _only_row(chain.run_id)
        assert row.status == "settled"
        assert row.points_reserved == 0
        assert row.points_settled == 10

        # 生成 Evidence
        async with SessionFactory() as db:
            evidence = await EvidenceWriter(db).get_by_tool_call_id(row.id)
            assert evidence is not None
    finally:
        await _teardown_chain(chain)


@pytest.mark.asyncio
async def test_p0_dnr_retry_unknown_reserves_and_reconciles() -> None:
    """DNR → retry unknown：第二次真实预留 10 分，调用行与钱包一致，reconcile 可 settle。"""
    chain = await _setup_chain(steps=3)
    try:
        transport = FakeMcpTransport([
            McpConnectionTimeout("connect timeout"),
            McpGatewayTimeout("gw timeout"),
        ])
        bridge = _bridge(transport)

        r1 = await bridge.execute(_context(chain, chain.step_ids[0]), {"keyword": "美妆"})
        assert r1.error_type == DEFINITELY_NOT_SENT
        w1 = await _wallet(chain.user_id)
        assert (w1.balance, w1.reserved) == (1000, 0)

        r2 = await bridge.execute(_context(chain, chain.step_ids[1]), {"keyword": "美妆"})
        assert r2.error_type == RESULT_UNKNOWN
        w2 = await _wallet(chain.user_id)
        assert (w2.balance, w2.reserved) == (990, 10)

        row = await _only_row(chain.run_id)
        assert row.status == "unknown"
        assert row.points_reserved == 10
        assert row.points_settled == 0

        # reconcile 确认成功 → settle
        snapshot = await bridge._coordinator.load_call(row.logical_call_id)
        assert snapshot is not None
        await bridge._coordinator.confirm_success(
            snapshot, validated_payload=OK_PAYLOAD, upstream_request_id="req-late", note="ok"
        )
        w3 = await _wallet(chain.user_id)
        assert (w3.balance, w3.reserved) == (990, 0)
    finally:
        await _teardown_chain(chain)


@pytest.mark.asyncio
async def test_p0_dnr_retry_succeeded_empty_settles_no_evidence() -> None:
    """DNR → retry succeeded_empty：第二次正常预留并结算，不生成 Evidence。"""
    chain = await _setup_chain(steps=3)
    try:
        transport = FakeMcpTransport([
            McpConnectionTimeout("connect timeout"),
            RemoteToolResult(structured_content=None, is_error=False, upstream_request_id="req-empty"),
        ])
        bridge = _bridge(transport)

        r1 = await bridge.execute(_context(chain, chain.step_ids[0]), {"keyword": "美妆"})
        assert r1.error_type == DEFINITELY_NOT_SENT

        r2 = await bridge.execute(_context(chain, chain.step_ids[1]), {"keyword": "美妆"})
        assert r2.status == "failed"
        assert r2.error_type == "succeeded_empty"
        w2 = await _wallet(chain.user_id)
        assert (w2.balance, w2.reserved) == (990, 0)

        row = await _only_row(chain.run_id)
        assert row.status == "failed"
        assert row.error_type == "succeeded_empty"
        assert row.points_reserved == 0
        assert row.points_settled == 10  # 结算

        async with SessionFactory() as db:
            evidence = await EvidenceWriter(db).get_by_tool_call_id(row.id)
            assert evidence is None

        # 回放一致：第三次跨 Step → failed + succeeded_empty
        r3 = await bridge.execute(_context(chain, chain.step_ids[2]), {"keyword": "美妆"})
        assert r3.status == "failed"
        assert r3.error_type == "succeeded_empty"
    finally:
        await _teardown_chain(chain)


@pytest.mark.asyncio
async def test_p0_dnr_retry_insufficient_balance_no_dispatch() -> None:
    """DNR → retry 前余额不足：prepare 检查余额失败，不发送第二次 MCP 请求。"""
    chain = await _setup_chain(balance=20, steps=3)
    try:
        transport = FakeMcpTransport([McpConnectionTimeout("connect timeout")])
        bridge = _bridge(transport)

        r1 = await bridge.execute(_context(chain, chain.step_ids[0]), {"keyword": "美妆"})
        assert r1.error_type == DEFINITELY_NOT_SENT
        w1 = await _wallet(chain.user_id)
        assert (w1.balance, w1.reserved) == (20, 0)

        # 用真实 WalletService 消耗余额（新增一个独立预留），使重试时余额不足
        async with SessionFactory.begin() as db:
            from app.billing.service import WalletService
            await WalletService(db).reserve(
                chain.user_id, 15, "drain-for-retry-test", "drain-1",
                reference_type="agent_tool_call",
                tenant_source=False,
            )
        w_drained = await _wallet(chain.user_id)
        assert (w_drained.balance, w_drained.reserved) == (5, 15)

        # 第二次 retry：balance=5 < 10 → 不派发
        r2 = await bridge.execute(_context(chain, chain.step_ids[1]), {"keyword": "美妆"})
        assert r2.status == "failed"
        assert r2.error_type == DEFINITELY_NOT_SENT
        assert len(transport.calls) == 1  # 未派发第二次
        w2 = await _wallet(chain.user_id)
        assert (w2.balance, w2.reserved) == (5, 15)
    finally:
        await _teardown_chain(chain)


@pytest.mark.asyncio
async def test_p0_same_dispatch_settle_is_idempotent() -> None:
    """同一 dispatch attempt 重复 settle 保持幂等：不重复扣费、调用行一致。"""
    chain = await _setup_chain(steps=2)
    try:
        transport = FakeMcpTransport([_ok_result()])
        bridge = _bridge(transport)

        r = await bridge.execute(_context(chain, chain.step_ids[0]), {"keyword": "美妆"})
        assert r.status == "success"
        row = await _only_row(chain.run_id)
        assert row.status == "settled"

        # 幂等重入 finalize_success：不重复扣费
        evidence_id, _ = await bridge._coordinator.finalize_success(
            logical_call_id=row.logical_call_id,
            user_id=chain.user_id,
            session_id=chain.session_id,
            validated_payload=OK_PAYLOAD,
            upstream_request_id="req-dup",
        )
        assert evidence_id is not None
        w = await _wallet(chain.user_id)
        assert (w.balance, w.reserved) == (990, 0)
    finally:
        await _teardown_chain(chain)


@pytest.mark.asyncio
async def test_p0_same_dispatch_release_is_idempotent() -> None:
    """同一 dispatch attempt 重复 release 保持幂等：不重复释放、调用行一致。"""
    chain = await _setup_chain(steps=2)
    try:
        transport = FakeMcpTransport([McpConnectionTimeout("connect timeout")])
        bridge = _bridge(transport)

        r = await bridge.execute(_context(chain, chain.step_ids[0]), {"keyword": "美妆"})
        assert r.error_type == DEFINITELY_NOT_SENT
        row = await _only_row(chain.run_id)

        # 幂等重入 finalize_release：不重复释放（钱包不变）
        await bridge._coordinator.finalize_release(
            logical_call_id=row.logical_call_id,
            user_id=chain.user_id,
            error_type=DEFINITELY_NOT_SENT,
            message="dup",
        )
        w = await _wallet(chain.user_id)
        assert (w.balance, w.reserved) == (1000, 0)
    finally:
        await _teardown_chain(chain)


@pytest.mark.asyncio
async def test_p1_retry_updates_step_ownership() -> None:
    """第二次派发后调用行 step_id 更新为当前 Step（恢复/Evidence 归属正确）。"""
    chain = await _setup_chain(steps=3)
    try:
        transport = FakeMcpTransport([
            McpConnectionTimeout("connect timeout"),
            _ok_result(),
        ])
        bridge = _bridge(transport)

        r1 = await bridge.execute(_context(chain, chain.step_ids[0]), {"keyword": "美妆"})
        assert r1.error_type == DEFINITELY_NOT_SENT

        r2 = await bridge.execute(_context(chain, chain.step_ids[1]), {"keyword": "美妆"})
        assert r2.status == "success"

        row = await _only_row(chain.run_id)
        assert row.step_id == chain.step_ids[1]  # 归属当前 Step
        assert row.dispatch_count == 2
    finally:
        await _teardown_chain(chain)


@pytest.mark.asyncio
async def test_p0_concurrent_retry_only_one_dispatch_wins() -> None:
    """并发恢复：两个并发同指纹重试，FOR UPDATE 锁保证只有一个获得第二次派发权。"""
    chain = await _setup_chain(steps=3)
    try:
        transport = FakeMcpTransport([
            McpConnectionTimeout("connect timeout"),
            _ok_result(),
        ])
        bridge = _bridge(transport)

        # 第一次派发失败
        r1 = await bridge.execute(_context(chain, chain.step_ids[0]), {"keyword": "美妆"})
        assert r1.error_type == DEFINITELY_NOT_SENT

        # 两个并发重试（step 1 和 step 2 同时）
        results = await asyncio.gather(
            bridge.execute(_context(chain, chain.step_ids[1]), {"keyword": "美妆"}),
            bridge.execute(_context(chain, chain.step_ids[2]), {"keyword": "美妆"}),
        )

        # 只有一个派发（+1 外发），另一个被防重回放
        # 顺序不定，但 transport.calls 总数 = 2（首次 + 一次重试）
        assert len(transport.calls) == 2
        # 至少一个是 success，另一个可能是 success 回放或 failed
        assert any(r.status == "success" for r in results)
        # 钱包只结算一次（不可重复扣费）
        w = await _wallet(chain.user_id)
        assert (w.balance, w.reserved) == (990, 0)
    finally:
        await _teardown_chain(chain)


# ---------------------------------------------------------------------------
# P1-2: MCP 首次返回 / Transcript 恢复消费同一统一有界模型视图
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p1_mcp_first_return_matches_transcript_recovery() -> None:
    """MCP 首次成功返回的 safe_summary 与 Transcript 崩溃恢复回放完全一致。"""
    chain = await _setup_chain()
    try:
        transport = FakeMcpTransport([_ok_result()])
        bridge = _bridge(transport)

        result = await bridge.execute(_context(chain), {"keyword": "美妆"})
        assert result.status == "success"
        first_view = result.safe_summary

        # 崩溃后接管：Transcript 回放 settled 调用 → 同一统一模型视图。
        async with SessionFactory() as db:
            run = await db.get(AgentRun, chain.run_id)
            transcript = await RunTranscriptLoader(db).load(run)
        results = _transcript_tool_results(transcript.messages)
        assert len(results) == 1
        assert results[0]["status"] == "success"
        assert results[0]["summary"] == first_view
        # 都必须是合法 JSON。
        json.loads(first_view)
        json.loads(results[0]["summary"])
    finally:
        await _teardown_chain(chain)


@pytest.mark.asyncio
async def test_p1_mcp_immediate_path_does_not_repeat_normalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP 即时成功路径只执行一次归一化（不经调用方二次 normalize）。"""
    from app.agent_runtime.normalization import NormalizationRegistry

    chain = await _setup_chain()
    calls = {"count": 0}
    original = NormalizationRegistry.normalize

    def counting(self, tool_name, payload):
        calls["count"] += 1
        return original(self, tool_name, payload)

    monkeypatch.setattr(NormalizationRegistry, "normalize", counting)
    try:
        transport = FakeMcpTransport([_ok_result()])
        bridge = _bridge(transport)
        result = await bridge.execute(_context(chain), {"keyword": "美妆"})
        assert result.status == "success"
        # 仅 finalize_success 内写 Evidence 时归一化一次；调用方不再二次执行。
        assert calls["count"] == 1
    finally:
        await _teardown_chain(chain)


# ---------------------------------------------------------------------------
# P1-4: 第二次 DNR 最终反馈原子持久化（DB 与返回一致，崩溃后 Transcript 仍 false）
# ---------------------------------------------------------------------------


async def _db_row_safe_feedback(run_id: str) -> dict:
    row = await _only_row(run_id)
    assert row.safe_error_message is not None
    return json.loads(row.safe_error_message)


@pytest.mark.asyncio
async def test_p1_first_dnr_db_and_return_both_retry_allowed() -> None:
    """第一次 DNR：数据库与返回均为 retry_allowed=true。"""
    chain = await _setup_chain()
    try:
        transport = FakeMcpTransport([McpConnectionTimeout("t1")])
        bridge = _bridge(transport)

        result = await bridge.execute(_context(chain), {"keyword": "美妆"})
        assert result.error_type == DEFINITELY_NOT_SENT
        assert json.loads(result.safe_summary)["same_fingerprint_retry_allowed"] is True
        # DB 持久化与返回一致。
        persisted = await _db_row_safe_feedback(chain.run_id)
        assert persisted["same_fingerprint_retry_allowed"] is True
        assert persisted["suggested_actions"] == [
            "可对相同参数重试一次；仍失败则调整参数、拆分平台或继续其他章节"
        ]
    finally:
        await _teardown_chain(chain)


@pytest.mark.asyncio
async def test_p1_second_dnr_db_and_return_both_exhausted() -> None:
    """第二次 DNR：数据库与返回均为 retry_allowed=false（exhausted 反馈原子持久化）。"""
    chain = await _setup_chain(steps=3)
    try:
        transport = FakeMcpTransport([
            McpConnectionTimeout("t1"),
            McpConnectionTimeout("t2"),
        ])
        bridge = _bridge(transport)

        r1 = await bridge.execute(_context(chain, chain.step_ids[0]), {"keyword": "美妆"})
        assert json.loads(r1.safe_summary)["same_fingerprint_retry_allowed"] is True

        r2 = await bridge.execute(_context(chain, chain.step_ids[1]), {"keyword": "美妆"})
        assert r2.error_type == DEFINITELY_NOT_SENT
        returned = json.loads(r2.safe_summary)
        assert returned["same_fingerprint_retry_allowed"] is False
        assert returned["suggested_actions"] == [
            "已用完一次重试：修改参数、拆分平台或更换工具后重试"
        ]
        # DB 持久化与返回完全相同。
        persisted = await _db_row_safe_feedback(chain.run_id)
        assert persisted == returned
        assert persisted["same_fingerprint_retry_allowed"] is False
    finally:
        await _teardown_chain(chain)


@pytest.mark.asyncio
async def test_p1_second_dnr_crash_before_step_output_transcript_still_false() -> None:
    """第二次 finalize 后、Step output 写入前崩溃：Transcript 恢复仍为 false。"""
    chain = await _setup_chain(steps=2)
    try:
        transport = FakeMcpTransport([
            McpConnectionTimeout("t1"),
            McpConnectionTimeout("t2"),
        ])
        bridge = _bridge(transport)

        r1 = await bridge.execute(_context(chain, chain.step_ids[0]), {"keyword": "美妆"})
        assert json.loads(r1.safe_summary)["same_fingerprint_retry_allowed"] is True
        r2 = await bridge.execute(_context(chain, chain.step_ids[1]), {"keyword": "美妆"})
        assert json.loads(r2.safe_summary)["same_fingerprint_retry_allowed"] is False
        # 此时 Step.output_json 尚未写入（协调器不更新 Step），模拟崩溃后接管。

        async with SessionFactory() as db:
            run = await db.get(AgentRun, chain.run_id)
            transcript = await RunTranscriptLoader(db).load(run)
        results = _transcript_tool_results(transcript.messages)
        # 最后一个 DNR（第二次派发所在的 Step）回放必须仍是 false。
        assert results[-1]["status"] == "failed"
        replayed = json.loads(results[-1]["summary"])
        assert replayed["same_fingerprint_retry_allowed"] is False
    finally:
        await _teardown_chain(chain)


@pytest.mark.asyncio
async def test_p1_third_same_fingerprint_not_dispatched_and_wallet_unchanged() -> None:
    """第三次相同指纹不外发；两次 DNR 均释放，钱包最终状态不变。"""
    chain = await _setup_chain(steps=4)
    try:
        transport = FakeMcpTransport([
            McpConnectionTimeout("t1"),
            McpConnectionTimeout("t2"),
        ])
        bridge = _bridge(transport)

        for step_id in chain.step_ids[:2]:
            result = await bridge.execute(_context(chain, step_id), {"keyword": "美妆"})
            assert result.error_type == DEFINITELY_NOT_SENT
        assert len(transport.calls) == 2

        r3 = await bridge.execute(_context(chain, chain.step_ids[3]), {"keyword": "美妆"})
        assert r3.error_type == DEFINITELY_NOT_SENT
        assert json.loads(r3.safe_summary)["same_fingerprint_retry_allowed"] is False
        assert len(transport.calls) == 2  # 第三次不外发

        wallet = await _wallet(chain.user_id)
        assert (wallet.balance, wallet.reserved) == (1000, 0)
    finally:
        await _teardown_chain(chain)
