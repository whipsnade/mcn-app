"""统一 Session Agent Engine 集成测试（设计文档 §四 / §4.1 / §七 / §11.3 / Task 14）。

用脚本化 ``AgentAction`` 的 fake 网关驱动引擎，使循环确定性；Reviewer 决策用
脚本化 ``ReviewDecision`` 驱动。覆盖：

1. 四种动作循环：ask_user / call_tool（计算 + MCP 桥 + 余额不足）/ submit_review
   （approve / revise / reject）/ complete；
2. 保护：50 决策暂停 + 恢复、取消信号、非法动作安全阈值；
3. 新鲜 Run：每条消息独立 Run，只有 paused 才能被 resume。
"""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select

from app.agent_artifacts.models import (
    AgentArtifactVersion,
    ArtifactDraft,
    ArtifactReviewBatch,
)
from app.agent_artifacts.service import ArtifactService
from app.agent_runtime.engine import MAX_INVALID_ACTIONS, AgentEngine, RunOutcome
from app.agent_runtime.events import AgentEventBroker, AgentEventStream
from app.agent_runtime.models import (
    AgentEvent,
    AgentMessage,
    AgentRun,
    AgentRunAttempt,
    AgentSession,
    AgentStep,
    AgentToolCall,
    EvidenceItem,
    MemoryEntry,
)
from app.agent_runtime.profiles import get_profile
from app.agent_runtime.repository import AgentRunRepository, utc_now
from app.agent_runtime.reviewer import ReviewDecision, ReviewIssue, ReviewerDriver
from app.agent_runtime.schemas import AskUser, CallTool, Complete, SubmitReview
from app.agent_runtime.state import InvalidRunTransition, RunStatus
from app.agent_runtime.tools.contracts import ToolResult
from app.agent_runtime.tools.calculation import CalculateExpressionTool
from app.agent_runtime.tools.mcp import AgentMcpTool
from app.agent_runtime.tools.registry import McpCatalogEntry, ToolRegistry
from app.billing.models import Wallet
from app.billing.service import InsufficientPointsError, WalletService
from app.mcp_gateway.contracts import DataTapService
from app.mcp_gateway.transport import RemoteToolResult
from app.model.contracts import ChatMessage

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
OK_PAYLOAD = {
    "result": json.dumps(
        {"rows": [{"keyword": "美妆", "volume": 123}], "total": 1},
        ensure_ascii=False,
    )
}
INTERNAL_NAME = "query_analysis_data"
REMOTE_NAME = "datatap.insight.query.analysis.v1"

# 无必需数字叶子的 payload：lineage 校验结果为空闭包，无需建 Evidence。
PAYLOAD_V1 = {"data": {"overview": {"brand": "瑞幸"}}}


# ---------------------------------------------------------------------------
# fake 网关 / 工具
# ---------------------------------------------------------------------------


class FakeAgentGateway:
    """返回脚本化 AgentAction；记录每次 decide 收到的消息。"""

    def __init__(self, actions: list[Any], *, on_decide: Any = None) -> None:
        self.actions = list(actions)
        self.calls: list[dict[str, Any]] = []
        self.on_decide = on_decide

    async def decide(
        self, *, run, attempt_id, profile, messages, thinking_sink=None, **kwargs
    ) -> Any:
        self.calls.append(
            {
                "run_id": run.id,
                "attempt_id": attempt_id,
                "profile": profile.full_name,
                "messages": list(messages),
                "thinking_sink": thinking_sink,
                **kwargs,
            }
        )
        if self.on_decide is not None:
            await self.on_decide(run, len(self.calls))
        if not self.actions:
            raise AssertionError("fake agent gateway exhausted")
        return self.actions.pop(0)


class FakeReviewerGateway:
    """返回脚本化 ReviewDecision。"""

    def __init__(self, decisions: list[ReviewDecision]) -> None:
        self.decisions = list(decisions)
        self.calls: list[dict[str, Any]] = []

    async def decide(
        self, *, run, attempt_id, profile, messages, thinking_sink=None, **kwargs
    ) -> Any:
        self.calls.append({"run_id": run.id, "messages": list(messages), **kwargs})
        if not self.decisions:
            raise AssertionError("fake reviewer gateway exhausted")
        return self.decisions.pop(0)


class NoopArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str = ""


class NoopTool:
    """零积分步进工具：只返回成功，不产生任何副作用。"""

    name = "noop_calc"
    input_model = NoopArgs
    points_cost = 0
    external_side_effect = False

    async def execute(self, context: Any, arguments: BaseModel) -> ToolResult:
        return ToolResult(status="success", safe_summary="noop ok")


class CountingTool:
    """零积分工具：记录实际外发次数，用于断言 decide→dispatch 间隙的取消拦截。"""

    def __init__(self, executed: list[Any]) -> None:
        self.executed = executed
        self.name = "counting_tool"
        self.input_model = NoopArgs
        self.points_cost = 0
        self.external_side_effect = False

    async def execute(self, context: Any, arguments: BaseModel) -> ToolResult:
        self.executed.append(arguments)
        return ToolResult(status="success", safe_summary="counted ok")


class RaisingMcpTool:
    """目录来源的 MCP 工具执行器：执行时直接抛 InsufficientPointsError。"""

    name = INTERNAL_NAME

    async def execute(self, context: Any, arguments: Any) -> ToolResult:
        raise InsufficientPointsError("insufficient points for MCP call")


class FakeMcpTransport:
    """记录调用并按序吐出预编排的 RemoteToolResult。"""

    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[DataTapService, str, dict[str, Any]]] = []

    async def call_tool(self, service: DataTapService, remote_name: str, arguments: Any):
        self.calls.append((service, remote_name, dict(arguments)))
        if not self.outcomes:
            raise AssertionError("unexpected transport dispatch")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _ok_result() -> RemoteToolResult:
    return RemoteToolResult(
        structured_content=OK_PAYLOAD, is_error=False, upstream_request_id="req-ok"
    )


def _mcp_registry(db_session, *, executor_factory: Any) -> ToolRegistry:
    entry = McpCatalogEntry(
        internal_tool_name=INTERNAL_NAME,
        service_slug="insight-cube-mcp",
        reviewed_description="query analysis data",
        input_schema_json=INPUT_SCHEMA,
        review_status="approved",
        is_enabled=True,
    )
    return ToolRegistry(catalog_source=[entry], mcp_executor_factory=executor_factory)


def _calc_registry(db_session) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(CalculateExpressionTool(db_session), category="calculation")
    return registry


def _noop_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(NoopTool(), category="calculation")
    return registry


# ---------------------------------------------------------------------------
# 运行/引擎装配
# ---------------------------------------------------------------------------


async def _new_run(db_session, *, user_id: str, session_id: str):
    repo = AgentRunRepository(db_session)
    run = AgentRun(
        id=str(uuid4()),
        session_id=session_id,
        user_id=user_id,
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
    attempt = await repo.begin_attempt(run.id)
    await repo.claim_lease(run.id, "worker", 300)
    return run, attempt


async def _setup_run(db_session, user_factory):
    user = await user_factory()
    now = utc_now()
    session = AgentSession(
        id=str(uuid4()),
        user_id=user.id,
        title="引擎测试会话",
        status="active",
        created_at=now,
        updated_at=now,
    )
    db_session.add(session)
    await db_session.flush()
    run, attempt = await _new_run(db_session, user_id=user.id, session_id=session.id)
    return run, attempt, user, session


def _make_engine(
    db_session,
    *,
    actions: list[Any],
    decisions: list[ReviewDecision],
    registry: ToolRegistry | None = None,
    on_decide: Any = None,
    worker: str = "worker",
) -> tuple[AgentEngine, FakeAgentGateway, FakeReviewerGateway]:
    gateway = FakeAgentGateway(actions, on_decide=on_decide)
    reviewer_gateway = FakeReviewerGateway(decisions)
    registry = registry or ToolRegistry()
    broker = AgentEventBroker()
    events = AgentEventStream(db_session, broker)
    reviewer = ReviewerDriver(db_session, reviewer_gateway, worker_id=worker)
    engine = AgentEngine(
        db_session,
        gateway=gateway,
        registry=registry,
        events=events,
        reviewer=reviewer,
        worker_id=worker,
    )
    return engine, gateway, reviewer_gateway


async def _make_draft(
    db_session,
    run: AgentRun,
    *,
    brand: str = "瑞幸",
    payload: dict[str, Any] | None = None,
    evidence_refs: list[dict[str, Any]] | None = None,
):
    service = ArtifactService(db_session)
    _, draft, revision = await service.create_or_get_draft(
        session_id=run.session_id,
        user_id=run.user_id,
        run_id=run.id,
        module="brand",
        business_fields={"brand": brand},
        schema_version="brand_report_v3",
        payload=payload if payload is not None else PAYLOAD_V1,
        evidence_refs=evidence_refs,
        artifact_type="brand_report_v3",
    )
    return service, draft, revision


# ---------------------------------------------------------------------------
# 1. 四种动作循环
# ---------------------------------------------------------------------------


async def test_ask_user_ends_clarification_with_pending_memory(
    db_session, user_factory
) -> None:
    run, attempt, _, _ = await _setup_run(db_session, user_factory)
    engine, _, _ = _make_engine(
        db_session,
        actions=[
            AskUser(action="ask_user", question="需要确认分析品牌", options=["瑞幸", "蜜雪冰城"]),
        ],
        decisions=[],
    )
    outcome = await engine.run(
        run=run,
        attempt_id=attempt.id,
        profile=get_profile("session_analyst_v1"),
        messages=[ChatMessage(role="user", content="帮我分析一个品牌")],
    )
    assert isinstance(outcome, RunOutcome)
    assert outcome.status == RunStatus.CLARIFICATION_REQUESTED
    assert run.status == RunStatus.CLARIFICATION_REQUESTED

    msg = await db_session.scalar(
        select(AgentMessage).where(AgentMessage.session_id == run.session_id)
    )
    assert msg is not None
    assert msg.role == "assistant"
    assert msg.content == "需要确认分析品牌"
    assert msg.run_id == run.id
    assert msg.metadata_json is not None
    assert msg.metadata_json["options"] == ["瑞幸", "蜜雪冰城"]

    memory = await db_session.scalar(
        select(MemoryEntry).where(MemoryEntry.memory_type == "pending_question")
    )
    assert memory is not None
    assert memory.source_run_id == run.id
    assert memory.content_json["question"] == "需要确认分析品牌"


async def test_complete_writes_assistant_message_with_suggestions(
    db_session, user_factory
) -> None:
    run, attempt, _, _ = await _setup_run(db_session, user_factory)
    engine, _, _ = _make_engine(
        db_session,
        actions=[Complete(action="complete", text="分析完成", suggestions=["继续看竞品"])],
        decisions=[],
    )
    outcome = await engine.run(
        run=run,
        attempt_id=attempt.id,
        profile=get_profile("session_analyst_v1"),
        messages=[ChatMessage(role="user", content="帮我看看品牌")],
    )
    assert outcome.status == RunStatus.COMPLETED
    assert run.status == RunStatus.COMPLETED
    msg = await db_session.scalar(
        select(AgentMessage).where(AgentMessage.session_id == run.session_id)
    )
    assert msg is not None
    assert msg.role == "assistant"
    assert msg.content == "分析完成"
    assert msg.metadata_json["type"] == "completion"
    assert msg.metadata_json["suggestions"] == ["继续看竞品"]


async def test_call_tool_feeds_result_back_and_zero_cost_calc(db_session, user_factory) -> None:
    run, attempt, _, _ = await _setup_run(db_session, user_factory)
    engine, gateway, _ = _make_engine(
        db_session,
        actions=[
            CallTool(
                action="call_tool",
                internal_tool_name="calculate_expression",
                arguments={"expression": "1+1", "variables": {}},
                rationale="计算",
            ),
            Complete(action="complete", text="结果是 2"),
        ],
        decisions=[],
        registry=_calc_registry(db_session),
    )
    outcome = await engine.run(
        run=run,
        attempt_id=attempt.id,
        profile=get_profile("session_analyst_v1"),
        messages=[ChatMessage(role="user", content="算一下 1+1")],
    )
    assert outcome.status == RunStatus.COMPLETED

    # 工具结果回喂进下一次 decide 的消息
    assert len(gateway.calls) == 2
    second_messages = gateway.calls[1]["messages"]
    assert any(
        m.role == "user" and "2" in m.content for m in second_messages
    )

    # 零积分：settled internal call，points == 0
    call = await db_session.scalar(
        select(AgentToolCall).where(AgentToolCall.run_id == run.id)
    )
    assert call is not None
    assert call.status == "settled"
    assert (call.points_settled, call.points_reserved) == (0, 0)

    # 工具事件持久化
    types = {
        row.event_type
        for row in (
            await db_session.scalars(
                select(AgentEvent).where(AgentEvent.run_id == run.id)
            )
        ).all()
    }
    assert {"tool.started", "tool.succeeded", "run.completed"} <= types


async def test_mcp_tool_through_bridge_settles_points(db_session, user_factory) -> None:
    run, attempt, user, _ = await _setup_run(db_session, user_factory)
    await WalletService(db_session).ensure_welcome_grant(user.id)
    transport = FakeMcpTransport([_ok_result()])
    registry = _mcp_registry(
        db_session,
        executor_factory=lambda row: AgentMcpTool(
            internal_name=row.internal_tool_name,
            service=DataTapService.INSIGHT_CUBE,
            remote_name=REMOTE_NAME,
            input_schema=row.input_schema_json,
            output_schema=OUTPUT_SCHEMA,
            db_session=db_session,
            transport=transport,
        ),
    )
    engine, _, _ = _make_engine(
        db_session,
        actions=[
            CallTool(
                action="call_tool",
                internal_tool_name=INTERNAL_NAME,
                arguments={"keyword": "美妆"},
                rationale="查询",
            ),
            Complete(action="complete", text="查询完成"),
        ],
        decisions=[],
        registry=registry,
    )
    outcome = await engine.run(
        run=run,
        attempt_id=attempt.id,
        profile=get_profile("session_analyst_v1"),
        messages=[ChatMessage(role="user", content="帮我查询美妆数据")],
    )
    assert outcome.status == RunStatus.COMPLETED
    assert len(transport.calls) == 1
    assert transport.calls[0][1] == REMOTE_NAME

    wallet = await WalletService(db_session).get_wallet(user.id)
    assert (wallet.balance, wallet.reserved) == (990, 0)
    call = await db_session.scalar(
        select(AgentToolCall).where(AgentToolCall.run_id == run.id)
    )
    assert call is not None
    assert call.status == "settled"
    assert call.points_settled == 10
    evidence = await db_session.scalar(
        select(EvidenceItem).where(EvidenceItem.run_id == run.id)
    )
    assert evidence is not None


async def test_insufficient_balance_is_structured_tool_error_and_restricted_complete(
    db_session, user_factory
) -> None:
    run, attempt, user, _ = await _setup_run(db_session, user_factory)
    db_session.add(
        Wallet(user_id=user.id, balance=0, reserved=0, version=0, updated_at=utc_now())
    )
    await db_session.flush()
    registry = _mcp_registry(db_session, executor_factory=lambda row: RaisingMcpTool())
    engine, gateway, _ = _make_engine(
        db_session,
        actions=[
            CallTool(
                action="call_tool",
                internal_tool_name=INTERNAL_NAME,
                arguments={"keyword": "美妆"},
                rationale="查询",
            ),
            Complete(action="complete", text="余额不足，仅能基于已有证据给出受限结论"),
        ],
        decisions=[],
        registry=registry,
    )
    outcome = await engine.run(
        run=run,
        attempt_id=attempt.id,
        profile=get_profile("session_analyst_v1"),
        messages=[ChatMessage(role="user", content="查询美妆数据")],
    )
    # 余额不足是结构化工具错误（非崩溃），模型可继续 complete 做受限交付
    assert outcome.status == RunStatus.COMPLETED
    assert len(gateway.calls) == 2
    assert any(
        "insufficient points" in m.content for m in gateway.calls[1]["messages"]
    )
    # 无残留 tool call 行、钱包不变
    assert (
        await db_session.scalar(
            select(func.count(AgentToolCall.id)).where(AgentToolCall.run_id == run.id)
        )
    ) == 0
    wallet = await WalletService(db_session).get_wallet(user.id)
    assert (wallet.balance, wallet.reserved) == (0, 0)


# ---------------------------------------------------------------------------
# submit_review：approve / revise / reject
# ---------------------------------------------------------------------------


async def test_submit_review_approve_publishes(db_session, user_factory) -> None:
    run, attempt, _, _ = await _setup_run(db_session, user_factory)
    _, draft, _ = await _make_draft(db_session, run)
    engine, _, _ = _make_engine(
        db_session,
        actions=[
            SubmitReview(
                action="submit_review",
                artifact_draft_ids=(draft.id,),
                completion_text="品牌分析完成",
                summary="瑞幸品牌分析",
            ),
        ],
        decisions=[ReviewDecision(decision="approve")],
    )
    outcome = await engine.run(
        run=run,
        attempt_id=attempt.id,
        profile=get_profile("session_analyst_v1"),
        messages=[ChatMessage(role="user", content="分析瑞幸品牌")],
    )
    assert outcome.status == RunStatus.COMPLETED
    assert run.status == RunStatus.COMPLETED

    msg = await db_session.scalar(
        select(AgentMessage).where(AgentMessage.session_id == run.session_id)
    )
    assert msg is not None
    assert msg.role == "assistant"
    assert msg.content == "品牌分析完成"
    assert msg.run_id == run.id
    # 发布路径的 assistant 消息 id 回传进 RunOutcome（与 ask_user/complete 一致）
    assert outcome.assistant_message_id == msg.id

    version = await db_session.scalar(
        select(AgentArtifactVersion).where(AgentArtifactVersion.source_run_id == run.id)
    )
    assert version is not None

    batch = await db_session.scalar(
        select(ArtifactReviewBatch).where(ArtifactReviewBatch.parent_run_id == run.id)
    )
    assert batch is not None
    assert batch.status == "completed"
    draft_row = await db_session.get(ArtifactDraft, draft.id)
    assert draft_row is not None
    assert draft_row.status == "idle"
    assert draft_row.owner_run_id is None

    # 发布后也发出 message.completed 事件（与 complete 路径一致）
    event_types = {
        row.event_type
        for row in (
            await db_session.scalars(
                select(AgentEvent).where(AgentEvent.run_id == run.id)
            )
        ).all()
    }
    assert "message.completed" in event_types


async def test_submit_review_revise_then_approve(db_session, user_factory) -> None:
    run, attempt, _, _ = await _setup_run(db_session, user_factory)
    _, draft, _ = await _make_draft(db_session, run)
    engine, gateway, _ = _make_engine(
        db_session,
        actions=[
            SubmitReview(
                action="submit_review",
                artifact_draft_ids=(draft.id,),
                completion_text="品牌分析完成",
                summary="瑞幸品牌分析",
            ),
            SubmitReview(
                action="submit_review",
                artifact_draft_ids=(draft.id,),
                completion_text="品牌分析完成",
                summary="瑞幸品牌分析",
            ),
        ],
        decisions=[
            ReviewDecision(
                decision="revise",
                issues=[ReviewIssue(code="missing_data", message="需要补查声量数据")],
            ),
            ReviewDecision(decision="approve"),
        ],
    )
    outcome = await engine.run(
        run=run,
        attempt_id=attempt.id,
        profile=get_profile("session_analyst_v1"),
        messages=[ChatMessage(role="user", content="分析瑞幸品牌")],
    )
    assert outcome.status == RunStatus.COMPLETED
    # 一个用户 Run 只存在一条 batch（复用，不重复创建）
    batches = (
        await db_session.scalars(
            select(ArtifactReviewBatch).where(ArtifactReviewBatch.parent_run_id == run.id)
        )
    ).all()
    assert len(batches) == 1
    assert batches[0].status == "completed"
    # revise 的问题回喂给了模型（第二次 decide 前可见）
    assert len(gateway.calls) == 2
    assert any(
        "review_revision_requested" in m.content
        for m in gateway.calls[1]["messages"]
    )
    version = await db_session.scalar(
        select(AgentArtifactVersion).where(AgentArtifactVersion.source_run_id == run.id)
    )
    assert version is not None


async def test_submit_review_reject_fails_run(db_session, user_factory) -> None:
    run, attempt, _, _ = await _setup_run(db_session, user_factory)
    _, draft, _ = await _make_draft(db_session, run)
    engine, _, _ = _make_engine(
        db_session,
        actions=[
            SubmitReview(
                action="submit_review",
                artifact_draft_ids=(draft.id,),
                completion_text="分析",
                summary="瑞幸品牌分析",
            ),
        ],
        decisions=[
            ReviewDecision(
                decision="reject",
                issues=[ReviewIssue(code="untrusted", message="数字无法追溯")],
            )
        ],
    )
    outcome = await engine.run(
        run=run,
        attempt_id=attempt.id,
        profile=get_profile("session_analyst_v1"),
        messages=[ChatMessage(role="user", content="分析瑞幸品牌")],
    )
    assert outcome.status == RunStatus.FAILED
    assert run.status == RunStatus.FAILED
    batch = await db_session.scalar(
        select(ArtifactReviewBatch).where(ArtifactReviewBatch.parent_run_id == run.id)
    )
    assert batch is not None
    assert batch.status == "failed"
    draft_row = await db_session.get(ArtifactDraft, draft.id)
    assert draft_row is not None
    assert draft_row.status == "failed"
    assert (
        await db_session.scalar(
            select(func.count(AgentArtifactVersion.id)).where(
                AgentArtifactVersion.source_run_id == run.id
            )
        )
    ) == 0


async def test_submit_review_with_invalid_lineage_fed_back_as_structured_error(
    db_session, user_factory
) -> None:
    """必需数字叶子的 Draft 缺 lineage：拒绝进入 Review，回喂模型修正（§10.4）。"""
    run, attempt, _, _ = await _setup_run(db_session, user_factory)
    payload = {"data": {"overview": {"total_volume": 100}}}
    _, draft, _ = await _make_draft(db_session, run, payload=payload)
    engine, gateway, _ = _make_engine(
        db_session,
        actions=[
            SubmitReview(
                action="submit_review",
                artifact_draft_ids=(draft.id,),
                completion_text="分析",
                summary="瑞幸品牌分析",
            ),
            Complete(action="complete", text="需要补全数据来源"),
        ],
        decisions=[],
    )
    outcome = await engine.run(
        run=run,
        attempt_id=attempt.id,
        profile=get_profile("session_analyst_v1"),
        messages=[ChatMessage(role="user", content="分析瑞幸品牌")],
    )
    assert outcome.status == RunStatus.COMPLETED
    assert run.status == RunStatus.COMPLETED
    # lineage 失败作为结构化 lineage_error 回喂，且不进入 Review
    assert len(gateway.calls) == 2
    assert any(
        "lineage_error" in m.content for m in gateway.calls[1]["messages"]
    )
    assert (
        await db_session.scalar(
            select(func.count(ArtifactReviewBatch.id)).where(
                ArtifactReviewBatch.parent_run_id == run.id
            )
        )
    ) == 0


async def test_submit_review_publish_failure_fails_run_and_releases_drafts(
    db_session, user_factory, monkeypatch
) -> None:
    """publish_batch 抛异常：不把 Run 卡在 reviewing，释放 Draft 并收口 failed。"""
    from app.agent_artifacts.service import ArtifactService, PublishBlocked

    run, attempt, _, _ = await _setup_run(db_session, user_factory)
    _, draft, _ = await _make_draft(db_session, run)

    async def boom(self, review_batch_id: str, *, worker_id: str):
        raise PublishBlocked("publish exploded")

    monkeypatch.setattr(ArtifactService, "publish_batch", boom)

    engine, _, _ = _make_engine(
        db_session,
        actions=[
            SubmitReview(
                action="submit_review",
                artifact_draft_ids=(draft.id,),
                completion_text="分析",
                summary="瑞幸品牌分析",
            ),
        ],
        decisions=[ReviewDecision(decision="approve")],
    )
    outcome = await engine.run(
        run=run,
        attempt_id=attempt.id,
        profile=get_profile("session_analyst_v1"),
        messages=[ChatMessage(role="user", content="分析瑞幸品牌")],
    )
    assert outcome.status == RunStatus.FAILED
    assert run.status == RunStatus.FAILED
    # working head 已释放（不是卡在 reviewing 持有者）
    draft_row = await db_session.get(ArtifactDraft, draft.id)
    assert draft_row is not None
    assert draft_row.status == "failed"
    assert draft_row.owner_run_id is None
    # 未产生任何版本（部分发布被拒绝）
    assert (
        await db_session.scalar(
            select(func.count(AgentArtifactVersion.id)).where(
                AgentArtifactVersion.source_run_id == run.id
            )
        )
    ) == 0


# ---------------------------------------------------------------------------
# 2. 保护：暂停 / 恢复 / 取消 / 非法动作
# ---------------------------------------------------------------------------


async def test_pause_at_decision_limit_and_resume(db_session, user_factory) -> None:
    run, attempt, _, _ = await _setup_run(db_session, user_factory)
    actions = [
        CallTool(
            action="call_tool",
            internal_tool_name="noop_calc",
            arguments={"value": str(index)},
            rationale="步进",
        )
        for index in range(50)
    ]
    engine, _, _ = _make_engine(
        db_session, actions=actions, decisions=[], registry=_noop_registry()
    )
    outcome = await engine.run(
        run=run,
        attempt_id=attempt.id,
        profile=get_profile("session_analyst_v1"),
        messages=[ChatMessage(role="user", content="跑满决策数")],
    )
    assert outcome.status == RunStatus.PAUSED
    assert run.status == RunStatus.PAUSED
    attempt_row = await db_session.get(AgentRunAttempt, attempt.id)
    assert attempt_row is not None
    assert attempt_row.decision_count == 50
    assert attempt_row.outcome == "paused"
    assert run.decision_count == 50

    # 用户恢复：begin_attempt(resumed=True) 新建 Attempt，计数从零
    repo = AgentRunRepository(db_session)
    attempt2 = await repo.begin_attempt(run.id, resumed=True)
    await repo.claim_lease(run.id, "worker", 300)
    assert attempt2.attempt == 2
    assert attempt2.decision_count == 0

    engine2, _, _ = _make_engine(
        db_session, actions=[Complete(action="complete", text="恢复完成")], decisions=[]
    )
    outcome2 = await engine2.run(
        run=run,
        attempt_id=attempt2.id,
        profile=get_profile("session_analyst_v1"),
        messages=[ChatMessage(role="user", content="继续")],
    )
    assert outcome2.status == RunStatus.COMPLETED
    attempt2_row = await db_session.get(AgentRunAttempt, attempt2.id)
    assert attempt2_row is not None
    assert attempt2_row.decision_count == 1
    assert run.decision_count == 51


async def test_expired_lease_fails_run_cleanly(db_session, user_factory) -> None:
    """租约过期后续租失败：干净收口 failed，而不是静默绕过 50 决策守卫。"""
    run, attempt, _, _ = await _setup_run(db_session, user_factory)
    # 让租约过期（模拟长 Attempt 期间租约到期 / heartbeat 丢失）
    run_row = await db_session.get(AgentRun, run.id)
    run_row.lease_expires_at = utc_now() - timedelta(seconds=1)
    await db_session.flush()

    engine, _, _ = _make_engine(
        db_session, actions=[Complete(action="complete", text="完成")], decisions=[]
    )
    outcome = await engine.run(
        run=run,
        attempt_id=attempt.id,
        profile=get_profile("session_analyst_v1"),
        messages=[ChatMessage(role="user", content="开始")],
    )
    assert outcome.status == RunStatus.FAILED
    assert run.status == RunStatus.FAILED
    assert run.error_code == "run_lease_lost"
    # open attempt 已收口（未卡在 running）
    attempt_row = await db_session.get(AgentRunAttempt, attempt.id)
    assert attempt_row is not None
    assert attempt_row.outcome == "failed"
    assert attempt_row.ended_at is not None


async def test_cancel_requested_stops_new_calls_and_settles_inflight(
    db_session, user_factory
) -> None:
    run, attempt, _, _ = await _setup_run(db_session, user_factory)
    repo = AgentRunRepository(db_session)
    executed: list[Any] = []
    registry = ToolRegistry()
    registry.register(CountingTool(executed), category="calculation")

    async def on_decide(run, index):
        if index == 2:
            await repo.request_cancel(run.id, run.user_id)

    engine, _, _ = _make_engine(
        db_session,
        actions=[
            CallTool(
                action="call_tool",
                internal_tool_name="counting_tool", arguments={"value": "a"}, rationale="x"
            ),
            CallTool(
                action="call_tool",
                internal_tool_name="counting_tool", arguments={"value": "b"}, rationale="x"
            ),
        ],
        decisions=[],
        registry=registry,
        on_decide=on_decide,
    )
    outcome = await engine.run(
        run=run,
        attempt_id=attempt.id,
        profile=get_profile("session_analyst_v1"),
        messages=[ChatMessage(role="user", content="开始任务")],
    )
    assert outcome.status == RunStatus.CANCELLED
    assert run.status == RunStatus.CANCELLED
    # 第一个工具已外发并结算；第二个在 decide→dispatch 间隙被取消拦截，未外发
    assert len(executed) == 1
    steps = (
        await db_session.scalars(
            select(AgentStep).where(
                AgentStep.run_id == run.id, AgentStep.step_type == "tool_call"
            )
        )
    ).all()
    assert len(steps) == 2
    assert [step.status for step in steps] == ["completed", "failed"]
    assert steps[1].output_json["error_type"] == "cancelled_not_sent"


async def test_cancel_between_decide_and_dispatch_blocks_tool_call(
    db_session, user_factory
) -> None:
    """decide（长模型调用）期间收到取消：外发前再核对，绝不发起工具调用（§11.3）。"""
    run, attempt, _, _ = await _setup_run(db_session, user_factory)
    repo = AgentRunRepository(db_session)
    executed: list[Any] = []
    registry = ToolRegistry()
    registry.register(CountingTool(executed), category="calculation")

    async def on_decide(run, index):
        if index == 1:
            await repo.request_cancel(run.id, run.user_id)

    engine, _, _ = _make_engine(
        db_session,
        actions=[
            CallTool(
                action="call_tool",
                internal_tool_name="counting_tool",
                arguments={"value": "x"},
                rationale="x",
            ),
        ],
        decisions=[],
        registry=registry,
        on_decide=on_decide,
    )
    outcome = await engine.run(
        run=run,
        attempt_id=attempt.id,
        profile=get_profile("session_analyst_v1"),
        messages=[ChatMessage(role="user", content="开始任务")],
    )
    assert outcome.status == RunStatus.CANCELLED
    assert run.status == RunStatus.CANCELLED
    # 工具从未真正外发
    assert executed == []
    step = await db_session.scalar(
        select(AgentStep).where(
            AgentStep.run_id == run.id, AgentStep.step_type == "tool_call"
        )
    )
    assert step is not None
    assert step.status == "failed"
    assert step.output_json["error_type"] == "cancelled_not_sent"


async def test_invalid_actions_reach_threshold_and_fail_run(
    db_session, user_factory
) -> None:
    run, attempt, _, _ = await _setup_run(db_session, user_factory)
    engine, gateway, _ = _make_engine(
        db_session,
        actions=[
            SimpleNamespace(action="teleport"),
            SimpleNamespace(action="teleport"),
            SimpleNamespace(action="teleport"),
        ],
        decisions=[],
    )
    outcome = await engine.run(
        run=run,
        attempt_id=attempt.id,
        profile=get_profile("session_analyst_v1"),
        messages=[ChatMessage(role="user", content="开始")],
    )
    assert outcome.status == RunStatus.FAILED
    assert run.status == RunStatus.FAILED
    assert len(gateway.calls) == MAX_INVALID_ACTIONS
    # 每次非法输出都作为 validation_error 结构化结果回喂
    assert all(
        any("validation_error" in m.content for m in call["messages"])
        for call in gateway.calls[1:]
    )


async def test_invalid_action_then_valid_recovers(db_session, user_factory) -> None:
    run, attempt, _, _ = await _setup_run(db_session, user_factory)
    engine, gateway, _ = _make_engine(
        db_session,
        actions=[
            SimpleNamespace(action="teleport"),
            Complete(action="complete", text="恢复完成"),
        ],
        decisions=[],
    )
    outcome = await engine.run(
        run=run,
        attempt_id=attempt.id,
        profile=get_profile("session_analyst_v1"),
        messages=[ChatMessage(role="user", content="开始")],
    )
    assert outcome.status == RunStatus.COMPLETED
    assert len(gateway.calls) == 2
    assert run.decision_count == 2


# ---------------------------------------------------------------------------
# 3. 新鲜 Run：每条消息独立，只有 paused 可恢复
# ---------------------------------------------------------------------------


async def test_fresh_run_per_message_and_only_paused_resumes(
    db_session, user_factory
) -> None:
    run_a, attempt_a, user, session = await _setup_run(db_session, user_factory)
    engine_a, _, _ = _make_engine(
        db_session, actions=[Complete(action="complete", text="A 完成")], decisions=[]
    )
    outcome_a = await engine_a.run(
        run=run_a,
        attempt_id=attempt_a.id,
        profile=get_profile("session_analyst_v1"),
        messages=[ChatMessage(role="user", content="消息 A")],
    )
    assert outcome_a.status == RunStatus.COMPLETED

    # 同一会话的后续消息创建新 Run，绝不复用已完成 Run 的执行卡
    run_b, attempt_b = await _new_run(
        db_session, user_id=user.id, session_id=session.id
    )
    engine_b, _, _ = _make_engine(
        db_session, actions=[Complete(action="complete", text="B 完成")], decisions=[]
    )
    outcome_b = await engine_b.run(
        run=run_b,
        attempt_id=attempt_b.id,
        profile=get_profile("session_analyst_v1"),
        messages=[ChatMessage(role="user", content="消息 B")],
    )
    assert outcome_b.status == RunStatus.COMPLETED
    assert outcome_b.run_id != outcome_a.run_id
    assert run_b.id != run_a.id

    # run_a 不被后续消息复用：decision_count 保持，assistant 消息只有一条
    fresh_a = await db_session.get(AgentRun, run_a.id)
    assert fresh_a is not None
    assert fresh_a.decision_count == 1
    a_messages = (
        await db_session.scalars(
            select(AgentMessage).where(AgentMessage.run_id == run_a.id)
        )
    ).all()
    assert len(a_messages) == 1

    # 只有 paused 才能被 resume：completed Run 不能 begin_attempt(resumed=True)
    repo = AgentRunRepository(db_session)
    with pytest.raises(InvalidRunTransition):
        await repo.begin_attempt(run_a.id, resumed=True)
