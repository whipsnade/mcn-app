"""统一 Session Agent Engine 集成测试（设计文档 §四 / §4.1 / §七 / §11.3 / Task 14）。

用脚本化 ``AgentAction`` 的 fake 网关驱动引擎，使循环确定性；Reviewer 决策用
脚本化 ``ReviewDecision`` 驱动。覆盖：

1. 四种动作循环：ask_user / call_tool（计算 + MCP 桥 + 余额不足）/ submit_review
   （approve / revise / reject）/ complete；
2. 保护：50 决策暂停 + 恢复、取消信号、非法动作安全阈值；
3. 新鲜 Run：每条消息独立 Run，只有 paused 才能被 resume。
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, func, select

from app.agent_artifacts.models import (
    AgentArtifact,
    AgentArtifactVersion,
    ArtifactDraft,
    ArtifactDraftRevision,
    ArtifactEvent,
    ArtifactReviewAttempt,
    ArtifactReviewBatch,
    ArtifactReviewItem,
)
from app.agent_artifacts.service import ArtifactService
from app.agent_runtime.engine import MAX_INVALID_ACTIONS, AgentEngine, RunOutcome
from app.agent_runtime.events import AgentEventBroker, AgentEventStream
from app.agent_runtime.model_gateway import AgentModelGateway
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
from app.agent_runtime.thinking import AgentEventThinkingSink
from app.agent_runtime.tools.artifacts import CreateDraftTool, UpdateDraftTool
from app.agent_runtime.tools.contracts import ToolResult
from app.agent_runtime.tools.calculation import CalculateExpressionTool
from app.agent_runtime.tools.mcp import AgentMcpTool
from app.agent_runtime.tools.registry import McpCatalogEntry, ToolRegistry
from app.billing.models import Wallet
from app.billing.service import InsufficientPointsError, WalletService
from app.db.session import SessionFactory
from app.db.session import engine as db_engine
from app.identity.models import User
from app.mcp_gateway.contracts import DataTapService
from app.mcp_gateway.transport import RemoteToolResult
from app.model.contracts import ChatMessage
from app.model.prompt_logs import PromptLogEntry
from app.model.tencent_plan import TencentPlanAdapter

from tests.agent_artifacts.payload_fixtures import insight_metric_payload, insight_payload

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

# 无必需数字叶子的合法 payload（insight markdown）：lineage 校验结果为空闭包，
# 无需建 Evidence；A5 起 Draft 必须过强类型校验。
PAYLOAD_V1 = insight_payload()


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


@asynccontextmanager
async def _shared_session(db_session):
    """与引擎共享同一夹具会话的 session_factory（durable 写入在同一连接内提交）。"""
    yield db_session


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
        module="insight",
        business_fields={"parent_artifact_version_id": "pv-1", "question": brand},
        schema_version="insight_board_v1",
        payload=payload if payload is not None else PAYLOAD_V1,
        evidence_refs=evidence_refs,
        artifact_type="insight_board_v1",
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
            session_factory=lambda: _shared_session(db_session),
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
    payload = insight_metric_payload(value=100)
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
    # 第一个工具已外发并结算；第二次 decide 后取消已到达，分发前被闸门拦截——
    # 第二个工具不外发、不产生任何新 Step
    assert len(executed) == 1
    steps = (
        await db_session.scalars(
            select(AgentStep).where(
                AgentStep.run_id == run.id, AgentStep.step_type == "tool_call"
            )
        )
    ).all()
    assert len(steps) == 1
    assert steps[0].status == "completed"
    # 恰好一个 run.cancelled 终态事件
    cancelled_events = [
        row
        for row in (
            await db_session.scalars(
                select(AgentEvent).where(AgentEvent.run_id == run.id)
            )
        ).all()
        if row.event_type == "run.cancelled"
    ]
    assert len(cancelled_events) == 1


async def test_cancel_between_decide_and_dispatch_blocks_tool_call(
    db_session, user_factory
) -> None:
    """decide（长模型调用）期间收到取消：分发前闸门拦截，绝不发起工具调用（§11.3/§5.5）。"""
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
    # 工具从未真正外发，也未产生 tool_call Step
    assert executed == []
    assert (
        await db_session.scalar(
            select(func.count(AgentStep.id)).where(
                AgentStep.run_id == run.id, AgentStep.step_type == "tool_call"
            )
        )
    ) == 0
    # 恰好一个 run.cancelled 终态事件
    cancelled_events = [
        row
        for row in (
            await db_session.scalars(
                select(AgentEvent).where(AgentEvent.run_id == run.id)
            )
        ).all()
        if row.event_type == "run.cancelled"
    ]
    assert len(cancelled_events) == 1


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


# ---------------------------------------------------------------------------
# 4. A5：Review Batch 集合冻结 / 幻觉 draft_id 回喂 / Draft 全出口释放
# ---------------------------------------------------------------------------


async def test_review_batch_draft_set_mismatch_added_draft(
    db_session, user_factory
) -> None:
    """首次 submit 冻结 Batch 集合后，新增 Draft 提交 → 结构化 mismatch 回喂，
    不建/不改 Batch，模型可继续（§5.7）。"""
    run, attempt, _, _ = await _setup_run(db_session, user_factory)
    _, draft_a, _ = await _make_draft(db_session, run, brand="瑞幸")
    _, draft_b, _ = await _make_draft(db_session, run, brand="库迪")
    engine, gateway, _ = _make_engine(
        db_session,
        actions=[
            SubmitReview(
                action="submit_review",
                artifact_draft_ids=(draft_a.id,),
                completion_text="品牌分析完成",
                summary="分析",
            ),
            SubmitReview(
                action="submit_review",
                artifact_draft_ids=(draft_a.id, draft_b.id),
                completion_text="品牌分析完成",
                summary="分析",
            ),
            Complete(action="complete", text="结束"),
        ],
        decisions=[
            ReviewDecision(
                decision="revise",
                issues=[ReviewIssue(code="missing_data", message="需要补查")],
            ),
        ],
    )
    outcome = await engine.run(
        run=run,
        attempt_id=attempt.id,
        profile=get_profile("session_analyst_v1"),
        messages=[ChatMessage(role="user", content="分析瑞幸品牌")],
    )
    assert outcome.status == RunStatus.COMPLETED
    # 只有一条 Batch，且未被改动（仍 pending 等原集合复审）
    batches = (
        await db_session.scalars(
            select(ArtifactReviewBatch).where(ArtifactReviewBatch.parent_run_id == run.id)
        )
    ).all()
    assert len(batches) == 1
    assert batches[0].status == "pending"
    # mismatch 回喂进第三次 decide 的消息
    assert len(gateway.calls) == 3
    assert any(
        "review_batch_draft_set_mismatch" in m.content for m in gateway.calls[2]["messages"]
    )


async def test_review_batch_draft_set_mismatch_missing_draft(
    db_session, user_factory
) -> None:
    """遗漏冻结集合中的 Draft → mismatch 回喂。"""
    run, attempt, _, _ = await _setup_run(db_session, user_factory)
    _, draft_a, _ = await _make_draft(db_session, run, brand="瑞幸")
    _, draft_b, _ = await _make_draft(db_session, run, brand="库迪")
    engine, gateway, _ = _make_engine(
        db_session,
        actions=[
            SubmitReview(
                action="submit_review",
                artifact_draft_ids=(draft_a.id, draft_b.id),
                completion_text="完成",
                summary="分析",
            ),
            SubmitReview(
                action="submit_review",
                artifact_draft_ids=(draft_a.id,),
                completion_text="完成",
                summary="分析",
            ),
            Complete(action="complete", text="结束"),
        ],
        decisions=[
            ReviewDecision(
                decision="revise",
                issues=[ReviewIssue(code="missing_data", message="需要补查")],
            ),
            ReviewDecision(
                decision="revise",
                issues=[ReviewIssue(code="missing_data", message="需要补查")],
            ),
        ],
    )
    outcome = await engine.run(
        run=run,
        attempt_id=attempt.id,
        profile=get_profile("session_analyst_v1"),
        messages=[ChatMessage(role="user", content="分析两个品牌")],
    )
    assert outcome.status == RunStatus.COMPLETED
    assert any(
        "review_batch_draft_set_mismatch" in m.content for m in gateway.calls[2]["messages"]
    )


async def test_review_batch_draft_set_mismatch_replaced_draft(
    db_session, user_factory
) -> None:
    """替换 Draft（放弃原 Draft 新建）→ mismatch 回喂，原 Batch 不受影响。"""
    run, attempt, _, _ = await _setup_run(db_session, user_factory)
    _, draft_a, _ = await _make_draft(db_session, run, brand="瑞幸")
    _, draft_b, _ = await _make_draft(db_session, run, brand="库迪")
    engine, gateway, _ = _make_engine(
        db_session,
        actions=[
            SubmitReview(
                action="submit_review",
                artifact_draft_ids=(draft_a.id,),
                completion_text="完成",
                summary="分析",
            ),
            SubmitReview(
                action="submit_review",
                artifact_draft_ids=(draft_b.id,),
                completion_text="完成",
                summary="分析",
            ),
            Complete(action="complete", text="结束"),
        ],
        decisions=[
            ReviewDecision(
                decision="revise",
                issues=[ReviewIssue(code="missing_data", message="需要补查")],
            ),
        ],
    )
    outcome = await engine.run(
        run=run,
        attempt_id=attempt.id,
        profile=get_profile("session_analyst_v1"),
        messages=[ChatMessage(role="user", content="分析瑞幸品牌")],
    )
    assert outcome.status == RunStatus.COMPLETED
    batches = (
        await db_session.scalars(
            select(ArtifactReviewBatch).where(ArtifactReviewBatch.parent_run_id == run.id)
        )
    ).all()
    assert len(batches) == 1
    assert any(
        "review_batch_draft_set_mismatch" in m.content for m in gateway.calls[2]["messages"]
    )


async def test_review_batch_mismatch_counts_as_invalid_and_fails_run(
    db_session, user_factory
) -> None:
    """连续 mismatch 计入无效动作：达到上限后 Run failed，不无限循环。"""
    run, attempt, _, _ = await _setup_run(db_session, user_factory)
    _, draft_a, _ = await _make_draft(db_session, run, brand="瑞幸")
    _, draft_b, _ = await _make_draft(db_session, run, brand="库迪")
    engine, gateway, _ = _make_engine(
        db_session,
        actions=[
            SubmitReview(
                action="submit_review",
                artifact_draft_ids=(draft_a.id,),
                completion_text="完成",
                summary="分析",
            ),
            *[
                SubmitReview(
                    action="submit_review",
                    artifact_draft_ids=(draft_b.id,),
                    completion_text="完成",
                    summary="分析",
                )
                for _ in range(MAX_INVALID_ACTIONS)
            ],
        ],
        decisions=[
            ReviewDecision(
                decision="revise",
                issues=[ReviewIssue(code="missing_data", message="需要补查")],
            ),
        ],
    )
    outcome = await engine.run(
        run=run,
        attempt_id=attempt.id,
        profile=get_profile("session_analyst_v1"),
        messages=[ChatMessage(role="user", content="分析瑞幸品牌")],
    )
    assert outcome.status == RunStatus.FAILED
    assert len(gateway.calls) == 1 + MAX_INVALID_ACTIONS
    assert any(
        "review_batch_draft_set_mismatch" in m.content for m in gateway.calls[-1]["messages"]
    )


async def test_submit_review_with_nonexistent_draft_id_fed_back_as_validation_error(
    db_session, user_factory
) -> None:
    """幻觉 draft_id（不存在）：结构化 validation_error 回喂并计入无效动作，
    不整 Run 崩溃；模型随后可正常完成。"""
    run, attempt, _, _ = await _setup_run(db_session, user_factory)
    engine, gateway, _ = _make_engine(
        db_session,
        actions=[
            SubmitReview(
                action="submit_review",
                artifact_draft_ids=("ghost-draft-id",),
                completion_text="完成",
                summary="分析",
            ),
            Complete(action="complete", text="改为直接回答"),
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
    assert len(gateway.calls) == 2
    assert any(
        "draft_not_found" in m.content for m in gateway.calls[1]["messages"]
    )
    # 未创建任何 Review Batch
    assert (
        await db_session.scalar(
            select(func.count(ArtifactReviewBatch.id)).where(
                ArtifactReviewBatch.parent_run_id == run.id
            )
        )
    ) == 0


async def test_submit_review_with_foreign_draft_id_fed_back_as_artifact_busy(
    db_session, user_factory
) -> None:
    """幻觉 draft_id（属于其他活动 Run）：结构化 artifact_busy 回喂，
    他人的 Draft 不被误标 reviewing、不建 Batch。"""
    run, attempt, user, session = await _setup_run(db_session, user_factory)
    run_b, _ = await _new_run(db_session, user_id=user.id, session_id=session.id)
    _, draft_b, _ = await _make_draft(db_session, run_b, brand="瑞幸")
    engine, gateway, _ = _make_engine(
        db_session,
        actions=[
            SubmitReview(
                action="submit_review",
                artifact_draft_ids=(draft_b.id,),
                completion_text="完成",
                summary="分析",
            ),
            Complete(action="complete", text="改为直接回答"),
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
    assert any(
        "artifact_busy" in m.content for m in gateway.calls[1]["messages"]
    )
    # 他人 Draft 不受影响：owner 不变、状态不进入 reviewing
    draft_row = await db_session.get(ArtifactDraft, draft_b.id)
    assert draft_row is not None
    assert draft_row.owner_run_id == run_b.id
    assert draft_row.status == "drafting"
    assert (
        await db_session.scalar(
            select(func.count(ArtifactReviewBatch.id)).where(
                ArtifactReviewBatch.parent_run_id == run.id
            )
        )
    ) == 0


async def test_ask_user_releases_owned_drafts_and_new_run_can_take_over(
    db_session, user_factory
) -> None:
    """模型建 Draft 后 ask_user：当前 Run 持有的 Draft 释放（idle，Revision 保留），
    新 Run 同 key 可立即接管（§5.7）。"""
    run, attempt, user, session = await _setup_run(db_session, user_factory)
    service, draft, _ = await _make_draft(db_session, run, brand="瑞幸")
    engine, _, _ = _make_engine(
        db_session,
        actions=[
            AskUser(action="ask_user", question="确认分析方向", options=["声量", "情感"]),
        ],
        decisions=[],
    )
    outcome = await engine.run(
        run=run,
        attempt_id=attempt.id,
        profile=get_profile("session_analyst_v1"),
        messages=[ChatMessage(role="user", content="分析瑞幸品牌")],
    )
    assert outcome.status == RunStatus.CLARIFICATION_REQUESTED
    draft_row = await db_session.get(ArtifactDraft, draft.id)
    assert draft_row is not None
    assert draft_row.owner_run_id is None
    assert draft_row.status == "idle"

    # 新 Run 同 key 可接管（不再 artifact_busy），Revision 在历史之上递增。
    run_b, _ = await _new_run(db_session, user_id=user.id, session_id=session.id)
    artifact_b, draft_b, revision_b = await service.create_or_get_draft(
        session_id=session.id,
        user_id=user.id,
        run_id=run_b.id,
        module="insight",
        business_fields={"parent_artifact_version_id": "pv-1", "question": "瑞幸"},
        schema_version="insight_board_v1",
        payload=PAYLOAD_V1,
        evidence_refs=[],
        artifact_type="insight_board_v1",
    )
    assert draft_b.id == draft.id
    assert draft_b.owner_run_id == run_b.id
    assert revision_b.revision == 2


async def test_complete_releases_owned_drafts(db_session, user_factory) -> None:
    """complete 出口（无正式产物）释放本 Run 持有的 Draft（§5.7）。"""
    run, attempt, _, _ = await _setup_run(db_session, user_factory)
    _, draft, _ = await _make_draft(db_session, run, brand="瑞幸")
    engine, _, _ = _make_engine(
        db_session,
        actions=[Complete(action="complete", text="直接回答完毕")],
        decisions=[],
    )
    outcome = await engine.run(
        run=run,
        attempt_id=attempt.id,
        profile=get_profile("session_analyst_v1"),
        messages=[ChatMessage(role="user", content="随便聊聊")],
    )
    assert outcome.status == RunStatus.COMPLETED
    draft_row = await db_session.get(ArtifactDraft, draft.id)
    assert draft_row is not None
    assert draft_row.owner_run_id is None
    assert draft_row.status == "idle"


async def test_pause_releases_owned_drafts(db_session, user_factory) -> None:
    """paused 出口释放本 Run 持有的 Draft（§5.7）。"""
    run, attempt, _, _ = await _setup_run(db_session, user_factory)
    _, draft, _ = await _make_draft(db_session, run, brand="瑞幸")
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
    draft_row = await db_session.get(ArtifactDraft, draft.id)
    assert draft_row is not None
    assert draft_row.owner_run_id is None
    assert draft_row.status == "idle"


async def test_run_failure_releases_owned_drafts_as_failed(
    db_session, user_factory
) -> None:
    """failed 出口（模型决策异常）释放本 Run 持有的 Draft（failed，§5.7）。"""
    run, attempt, _, _ = await _setup_run(db_session, user_factory)
    _, draft, _ = await _make_draft(db_session, run, brand="瑞幸")

    async def boom(_run, _index) -> None:
        raise RuntimeError("model exploded")

    engine, _, _ = _make_engine(
        db_session,
        actions=[Complete(action="complete", text="不会到达")],
        decisions=[],
        on_decide=boom,
    )
    outcome = await engine.run(
        run=run,
        attempt_id=attempt.id,
        profile=get_profile("session_analyst_v1"),
        messages=[ChatMessage(role="user", content="开始")],
    )
    assert outcome.status == RunStatus.FAILED
    draft_row = await db_session.get(ArtifactDraft, draft.id)
    assert draft_row is not None
    assert draft_row.owner_run_id is None
    assert draft_row.status == "failed"


async def test_reviewer_calls_use_artifact_reviewer_purpose(
    db_session, user_factory
) -> None:
    """Reviewer 调用的审计 purpose 是 artifact_reviewer（不再误标 agent_loop）。"""
    run, attempt, _, _ = await _setup_run(db_session, user_factory)
    _, draft, _ = await _make_draft(db_session, run)
    engine, _, reviewer_gateway = _make_engine(
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
    assert len(reviewer_gateway.calls) == 1
    assert reviewer_gateway.calls[0]["purpose"] == "artifact_reviewer"


# ---------------------------------------------------------------------------
# 5. A6：Profile allowed_actions 运行时强制（§5.8）
# ---------------------------------------------------------------------------


async def test_disallowed_action_fed_back_as_validation_error_and_recovers(
    db_session, user_factory
) -> None:
    """kol_detail_v1 不允许 ask_user：结构化 validation_error 回喂并计入无效动作，
    不分发、不写澄清消息/ pending Memory；模型随后合法 complete 正常收尾（§5.8）。"""
    run, attempt, _, _ = await _setup_run(db_session, user_factory)
    engine, gateway, _ = _make_engine(
        db_session,
        actions=[
            AskUser(action="ask_user", question="需要确认详情范围", options=["概览", "全量"]),
            Complete(action="complete", text="直接给出达人详情"),
        ],
        decisions=[],
    )
    outcome = await engine.run(
        run=run,
        attempt_id=attempt.id,
        profile=get_profile("kol_detail_v1"),
        messages=[ChatMessage(role="user", content="查看达人详情")],
    )
    assert outcome.status == RunStatus.COMPLETED
    assert len(gateway.calls) == 2
    # 不允许的动作作为结构化 validation_error 回喂（含稳定 code 与动作名）
    feedback = gateway.calls[1]["messages"][-1]
    assert feedback.role == "user"
    assert "validation_error" in feedback.content
    assert "action_not_allowed" in feedback.content
    assert "ask_user" in feedback.content
    # 未分发 ask_user：只有 complete 落的一条 assistant 消息，无澄清消息
    messages = (
        await db_session.scalars(
            select(AgentMessage).where(AgentMessage.run_id == run.id)
        )
    ).all()
    assert len(messages) == 1
    assert messages[0].metadata_json["type"] == "completion"
    assert (
        await db_session.scalar(
            select(func.count(MemoryEntry.id)).where(
                MemoryEntry.memory_type == "pending_question"
            )
        )
    ) == 0


async def test_disallowed_action_reaches_threshold_and_fails_run(
    db_session, user_factory
) -> None:
    """连续输出 Profile 不允许的动作：达到统一无效动作上限后 Run failed（§5.8）。"""
    run, attempt, _, _ = await _setup_run(db_session, user_factory)
    engine, gateway, _ = _make_engine(
        db_session,
        actions=[
            AskUser(action="ask_user", question=f"问题{index}", options=["是", "否"])
            for index in range(MAX_INVALID_ACTIONS)
        ],
        decisions=[],
    )
    outcome = await engine.run(
        run=run,
        attempt_id=attempt.id,
        profile=get_profile("kol_detail_v1"),
        messages=[ChatMessage(role="user", content="查看达人详情")],
    )
    assert outcome.status == RunStatus.FAILED
    assert run.status == RunStatus.FAILED
    assert len(gateway.calls) == MAX_INVALID_ACTIONS
    assert all(
        any("validation_error" in m.content for m in call["messages"])
        for call in gateway.calls[1:]
    )
    # 达上限的失败路径不收口为 clarification，澄清消息绝不落库
    assert (
        await db_session.scalar(
            select(func.count(AgentMessage.id)).where(AgentMessage.run_id == run.id)
        )
    ) == 0


async def test_disallowed_action_count_resets_after_valid_interaction(
    db_session, user_factory
) -> None:
    """不允许动作与合法动作交替：有效交互清零计数，两次间隔的违规不致 Run 失败。"""
    run, attempt, _, _ = await _setup_run(db_session, user_factory)
    engine, gateway, _ = _make_engine(
        db_session,
        actions=[
            AskUser(action="ask_user", question="违规1", options=["是", "否"]),
            CallTool(
                action="call_tool",
                internal_tool_name="noop_calc",
                arguments={"value": "x"},
                rationale="步进",
            ),
            AskUser(action="ask_user", question="违规2", options=["是", "否"]),
            Complete(action="complete", text="完成"),
        ],
        decisions=[],
        registry=_noop_registry(),
    )
    outcome = await engine.run(
        run=run,
        attempt_id=attempt.id,
        profile=get_profile("kol_detail_v1"),
        messages=[ChatMessage(role="user", content="查看达人详情")],
    )
    assert outcome.status == RunStatus.COMPLETED
    assert len(gateway.calls) == 4


# ---------------------------------------------------------------------------
# 6. A6：非法模型输出分层容错（§5.8，真实网关 + 脚本化供应商）
# ---------------------------------------------------------------------------


class _FakeCompletions:
    """脚本化 OpenAI completions 客户端：按序吐出流式响应或抛出异常。"""

    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self.outcomes:
            raise AssertionError("fake completions exhausted")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _CaptureLogWriter:
    def __init__(self) -> None:
        self.entries: list[PromptLogEntry] = []

    async def __call__(self, entry: PromptLogEntry) -> None:
        self.entries.append(entry)


def _stream_chunks(
    *, content_chunks: list[str | None], reasoning_chunks: list[str | None]
) -> Any:
    """生成带最终 usage 块和 finish_reason=stop 的流式响应。"""
    chunks = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content=content, reasoning_content=reasoning),
                    finish_reason=None,
                )
            ],
            usage=None,
            _request_id="req-stream",
        )
        for content, reasoning in zip(content_chunks, reasoning_chunks, strict=True)
    ]
    chunks.append(
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content=None, reasoning_content=None),
                    finish_reason="stop",
                )
            ],
            usage=None,
            _request_id="req-stream",
        )
    )

    async def stream() -> Any:
        for chunk in chunks:
            yield chunk

    return stream()


class _RecordingGateway:
    """记录 decide 入参后委托真实网关（回喂内容断言用）。"""

    def __init__(self, real: AgentModelGateway) -> None:
        self.real = real
        self.calls: list[dict[str, Any]] = []

    async def decide(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return await self.real.decide(**kwargs)


def _make_engine_with_real_gateway(
    db_session, client: _FakeCompletions, writer: _CaptureLogWriter
) -> tuple[AgentEngine, _RecordingGateway]:
    adapter = TencentPlanAdapter(
        client=client, log_writer=writer, stream_support_cache={}
    )
    gateway = _RecordingGateway(AgentModelGateway(adapter, db=db_session))
    broker = AgentEventBroker()
    events = AgentEventStream(db_session, broker)
    reviewer = ReviewerDriver(db_session, FakeReviewerGateway([]), worker_id="worker")
    engine = AgentEngine(
        db_session,
        gateway=gateway,
        registry=ToolRegistry(),
        events=events,
        reviewer=reviewer,
        worker_id="worker",
    )
    return engine, gateway


_BROKEN_JSON = '{"action":"complete","text":"unterminated'
_COMPLETE_JSON = '{"action":"complete","text":"分析完成"}'


async def test_unrepairable_model_output_counted_and_run_recovers(
    db_session, user_factory
) -> None:
    """一次修复后仍非法的输出：网关交回可恢复结果，引擎计数回喂后 Run 继续完成。"""
    run, attempt, _, _ = await _setup_run(db_session, user_factory)
    writer = _CaptureLogWriter()
    client = _FakeCompletions(
        [
            _stream_chunks(content_chunks=[_BROKEN_JSON], reasoning_chunks=[None]),
            _stream_chunks(content_chunks=[_BROKEN_JSON], reasoning_chunks=[None]),
            _stream_chunks(content_chunks=[_COMPLETE_JSON], reasoning_chunks=[None]),
        ]
    )
    engine, gateway = _make_engine_with_real_gateway(db_session, client, writer)
    outcome = await engine.run(
        run=run,
        attempt_id=attempt.id,
        profile=get_profile("session_analyst_v1"),
        messages=[ChatMessage(role="user", content="分析品牌")],
    )
    assert outcome.status == RunStatus.COMPLETED
    assert len(gateway.calls) == 2
    # 适配器单次修复语义保留：第一次 decide 内 2 次供应商调用，第二次 1 次
    assert len(client.calls) == 3
    # 坏输出作为 validation_error 回喂进第二次 decide，且计入决策计数
    assert any(
        m.role == "user" and "validation_error" in m.content
        for m in gateway.calls[1]["messages"]
    )
    assert run.decision_count == 2
    # prompt 学习日志：invalid 与 success 各一条
    assert [entry.status for entry in writer.entries] == ["invalid", "success"]


async def test_unrepairable_model_output_three_times_fails_run(
    db_session, user_factory
) -> None:
    """连续修复后仍非法：达到统一上限（3 次）后 Run failed，不再无界重试。"""
    run, attempt, _, _ = await _setup_run(db_session, user_factory)
    writer = _CaptureLogWriter()
    client = _FakeCompletions(
        [
            _stream_chunks(content_chunks=[_BROKEN_JSON], reasoning_chunks=[None])
            for _ in range(MAX_INVALID_ACTIONS * 2)
        ]
    )
    engine, gateway = _make_engine_with_real_gateway(db_session, client, writer)
    outcome = await engine.run(
        run=run,
        attempt_id=attempt.id,
        profile=get_profile("session_analyst_v1"),
        messages=[ChatMessage(role="user", content="分析品牌")],
    )
    assert outcome.status == RunStatus.FAILED
    assert run.status == RunStatus.FAILED
    assert len(gateway.calls) == MAX_INVALID_ACTIONS
    assert len(client.calls) == MAX_INVALID_ACTIONS * 2
    assert all(entry.status == "invalid" for entry in writer.entries)
    event_types = [
        row.event_type
        for row in (
            await db_session.scalars(
                select(AgentEvent).where(AgentEvent.run_id == run.id)
            )
        ).all()
    ]
    assert "run.failed" in event_types


async def test_provider_error_fails_run_immediately_without_invalid_counting(
    db_session, user_factory
) -> None:
    """供应商/协议错误属系统错误：直接收口 failed，不进入无效动作计数重试。"""
    run, attempt, _, _ = await _setup_run(db_session, user_factory)
    writer = _CaptureLogWriter()
    client = _FakeCompletions([RuntimeError("provider exploded")])
    engine, gateway = _make_engine_with_real_gateway(db_session, client, writer)
    outcome = await engine.run(
        run=run,
        attempt_id=attempt.id,
        profile=get_profile("session_analyst_v1"),
        messages=[ChatMessage(role="user", content="分析品牌")],
    )
    assert outcome.status == RunStatus.FAILED
    assert run.status == RunStatus.FAILED
    # 系统错误不进入容错循环：只调一次 decide、不重试供应商
    assert len(gateway.calls) == 1
    assert len(client.calls) == 1
    assert [entry.status for entry in writer.entries] == ["failed"]
    assert run.decision_count == 0


# ---------------------------------------------------------------------------
# 7. A6：thinking 实时事件（§5.8/§10.5，真实网关 + AgentEventThinkingSink）
# ---------------------------------------------------------------------------


async def test_thinking_events_flow_from_real_gateway_through_sink(
    db_session, user_factory
) -> None:
    """供应商真实返回 thinking：持久化为 thinking.started/delta/completed 事件。"""
    run, attempt, _, _ = await _setup_run(db_session, user_factory)
    writer = _CaptureLogWriter()
    client = _FakeCompletions(
        [
            _stream_chunks(
                content_chunks=[None, _COMPLETE_JSON],
                reasoning_chunks=["先想一步", None],
            )
        ]
    )
    engine, _ = _make_engine_with_real_gateway(db_session, client, writer)
    sink = engine.thinking_sink_for(run)
    assert isinstance(sink, AgentEventThinkingSink)
    outcome = await engine.run(
        run=run,
        attempt_id=attempt.id,
        profile=get_profile("session_analyst_v1"),
        messages=[ChatMessage(role="user", content="分析品牌")],
        thinking_sink=sink,
    )
    assert outcome.status == RunStatus.COMPLETED
    rows = (
        await db_session.scalars(
            select(AgentEvent)
            .where(AgentEvent.run_id == run.id)
            .order_by(AgentEvent.sequence)
        )
    ).all()
    types = [row.event_type for row in rows]
    assert types == [
        "run.started",
        "thinking.started",
        "thinking.delta",
        "thinking.completed",
        "message.completed",
        "run.completed",
    ]
    delta = rows[2]
    assert delta.payload_json["text"] == "先想一步"
    assert delta.payload_json["attempt"] == 1
    assert delta.payload_json["run_id"] == run.id


async def test_no_thinking_from_provider_emits_no_thinking_events(
    db_session, user_factory
) -> None:
    """供应商无 thinking：即使注入 sink 也零 thinking.* 事件（§10.5 门控语义）。"""
    run, attempt, _, _ = await _setup_run(db_session, user_factory)
    writer = _CaptureLogWriter()
    client = _FakeCompletions(
        [_stream_chunks(content_chunks=[_COMPLETE_JSON], reasoning_chunks=[None])]
    )
    engine, _ = _make_engine_with_real_gateway(db_session, client, writer)
    outcome = await engine.run(
        run=run,
        attempt_id=attempt.id,
        profile=get_profile("session_analyst_v1"),
        messages=[ChatMessage(role="user", content="分析品牌")],
        thinking_sink=engine.thinking_sink_for(run),
    )
    assert outcome.status == RunStatus.COMPLETED
    types = [
        row.event_type
        for row in (
            await db_session.scalars(
                select(AgentEvent)
                .where(AgentEvent.run_id == run.id)
                .order_by(AgentEvent.sequence)
            )
        ).all()
    ]
    assert types == ["run.started", "message.completed", "run.completed"]


async def test_thinking_sink_for_internal_run_returns_none(
    db_session, user_factory
) -> None:
    """Reviewer/Utility 内部 Run 不注入 thinking sink（visibility != user）。"""
    run, attempt, _, _ = await _setup_run(db_session, user_factory)
    engine, _, _ = _make_engine(db_session, actions=[], decisions=[])
    assert engine.thinking_sink_for(run) is not None
    run.visibility = "internal"
    assert engine.thinking_sink_for(run) is None


# ---------------------------------------------------------------------------
# 8. A6：事件顺序（§5.8：assistant message → message.completed → 终态事件最后）
# ---------------------------------------------------------------------------


async def _event_types(db_session, run_id: str) -> list[str]:
    return [
        row.event_type
        for row in (
            await db_session.scalars(
                select(AgentEvent)
                .where(AgentEvent.run_id == run_id)
                .order_by(AgentEvent.sequence)
            )
        ).all()
    ]


async def test_complete_emits_message_completed_before_run_completed(
    db_session, user_factory
) -> None:
    """complete 路径：message.completed 先于 run.completed，终态事件最后（§5.8）。"""
    run, attempt, _, _ = await _setup_run(db_session, user_factory)
    engine, _, _ = _make_engine(
        db_session,
        actions=[Complete(action="complete", text="分析完成")],
        decisions=[],
    )
    outcome = await engine.run(
        run=run,
        attempt_id=attempt.id,
        profile=get_profile("session_analyst_v1"),
        messages=[ChatMessage(role="user", content="分析品牌")],
    )
    assert outcome.status == RunStatus.COMPLETED
    types = await _event_types(db_session, run.id)
    assert types[-2:] == ["message.completed", "run.completed"]


async def test_approve_publish_emits_message_completed_before_run_completed(
    db_session, user_factory
) -> None:
    """approve 发布路径：review/artifact 事件 → message.completed → run.completed。"""
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
    types = await _event_types(db_session, run.id)
    assert types[-2:] == ["message.completed", "run.completed"]
    assert types.index("review.approved") < types.index("message.completed")


async def _create_committed_agent_run() -> tuple[str, str, str]:
    """在独立事务中提交 user/session/run，供跨会话在线消费测试使用。"""
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
            title="在线消费测试会话",
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
            profile_name="session_analyst_v1",
            profile_version="v1",
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


async def _purge_committed_agent_run(user_id: str, session_id: str, run_id: str) -> None:
    """删除在线消费测试提交的行，保持测试库干净。"""
    async with db_engine.begin() as conn:
        await conn.execute(delete(AgentEvent).where(AgentEvent.run_id == run_id))
        await conn.execute(delete(AgentMessage).where(AgentMessage.run_id == run_id))
        await conn.execute(delete(AgentStep).where(AgentStep.run_id == run_id))
        await conn.execute(
            delete(AgentRunAttempt).where(AgentRunAttempt.run_id == run_id)
        )
        await conn.execute(delete(AgentRun).where(AgentRun.id == run_id))
        await conn.execute(delete(AgentSession).where(AgentSession.id == session_id))
        await conn.execute(delete(User).where(User.id == user_id))


async def test_live_stream_delivers_message_completed_before_run_completed() -> None:
    """在线消费（reader 独立会话）：message.completed 先于 run.completed 到达，
    流在终态事件收口——终态事件必须是该 Run 最后一条用户可见事件（§5.8）。"""
    user_id, session_id, run_id = await _create_committed_agent_run()
    broker = AgentEventBroker()
    try:
        async with SessionFactory() as writer:
            repo = AgentRunRepository(writer)
            attempt = await repo.begin_attempt(run_id)
            assert await repo.claim_lease(run_id, "worker", 300)
            engine = AgentEngine(
                writer,
                gateway=FakeAgentGateway([Complete(action="complete", text="完成")]),
                registry=ToolRegistry(),
                events=AgentEventStream(writer, broker),
                reviewer=ReviewerDriver(
                    writer, FakeReviewerGateway([]), worker_id="worker"
                ),
                worker_id="worker",
            )
            run = await writer.get(AgentRun, run_id)
            assert run is not None

            received: list[str] = []

            async def consume() -> None:
                async with SessionFactory() as reader:
                    stream = AgentEventStream(reader, broker)
                    async for event in stream.stream(run_id, user_id, last_event_id=0):
                        received.append(event.event_type)

            consumer = asyncio.create_task(consume())
            await asyncio.sleep(0.05)  # reader 完成重放并进入 broker 等待
            outcome = await engine.run(
                run=run,
                attempt_id=attempt.id,
                profile=get_profile("session_analyst_v1"),
                messages=[ChatMessage(role="user", content="分析品牌")],
            )
            assert outcome.status == RunStatus.COMPLETED
            await asyncio.wait_for(consumer, timeout=5)
    finally:
        await _purge_committed_agent_run(user_id, session_id, run_id)

    # 旧顺序（run.completed 先发）会让在线客户端永远收不到 message.completed
    assert received[-1] == "run.completed"
    assert "message.completed" in received
    assert received.index("message.completed") < received.index("run.completed")


# ---------------------------------------------------------------------------
# 9. G1：终态事件统一收口（§5.8：恰好一个终态事件，且为该 Run 最后一条
#    用户可见事件）+ artifact 事件接入统一 Run SSE（§15.3）
# ---------------------------------------------------------------------------

_TERMINAL_TYPES = ("run.completed", "run.failed", "run.cancelled")


async def _terminal_events(db_session, run_id: str) -> list[AgentEvent]:
    return [
        row
        for row in (
            await db_session.scalars(
                select(AgentEvent)
                .where(AgentEvent.run_id == run_id)
                .order_by(AgentEvent.sequence)
            )
        ).all()
        if row.event_type in _TERMINAL_TYPES
    ]


async def test_submit_review_reject_emits_run_failed_as_last_event(
    db_session, user_factory
) -> None:
    """Reviewer reject：review.rejected 之后必须发 run.failed 终态事件（G1/P0）。

    终态事件是该 Run 最后一条用户可见事件；缺失会让 SSE 流不结束、前端
    Run 卡停在中间态。reject 前 Run 已被 Reviewer 迁移 failed，事件由引擎补发。
    """
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
    types = await _event_types(db_session, run.id)
    assert types[-1] == "run.failed"
    assert types.index("review.rejected") < types.index("run.failed")
    terminal = await _terminal_events(db_session, run.id)
    assert len(terminal) == 1
    assert terminal[0].payload_json["error_code"] == "review_rejected"


async def test_third_revise_mapped_to_reject_emits_run_failed(
    db_session, user_factory
) -> None:
    """第 3 次 revise 按 reject 处理：同样以 run.failed 终态事件收尾（G1/P0）。"""
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
            SubmitReview(
                action="submit_review",
                artifact_draft_ids=(draft.id,),
                completion_text="分析",
                summary="瑞幸品牌分析",
            ),
            SubmitReview(
                action="submit_review",
                artifact_draft_ids=(draft.id,),
                completion_text="分析",
                summary="瑞幸品牌分析",
            ),
        ],
        decisions=[
            ReviewDecision(
                decision="revise",
                issues=[ReviewIssue(code="missing_data", message="需要补查")],
            ),
            ReviewDecision(
                decision="revise",
                issues=[ReviewIssue(code="missing_data", message="仍需补查")],
            ),
            ReviewDecision(
                decision="revise",
                issues=[ReviewIssue(code="missing_data", message="仍未补查")],
            ),
        ],
    )
    outcome = await engine.run(
        run=run,
        attempt_id=attempt.id,
        profile=get_profile("session_analyst_v1"),
        messages=[ChatMessage(role="user", content="分析瑞幸品牌")],
    )
    assert outcome.status == RunStatus.FAILED
    types = await _event_types(db_session, run.id)
    assert types[-1] == "run.failed"
    assert types.index("review.rejected") < types.index("run.failed")
    terminal = await _terminal_events(db_session, run.id)
    assert len(terminal) == 1
    assert terminal[0].payload_json["error_code"] == "review_rejected"


async def test_publish_failure_emits_run_failed_with_error_code(
    db_session, user_factory, monkeypatch
) -> None:
    """publish_batch 异常：run.failed 终态事件带稳定 error_code=publish_error（G1）。"""
    from app.agent_artifacts.service import PublishBlocked

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
    types = await _event_types(db_session, run.id)
    assert types[-1] == "run.failed"
    terminal = await _terminal_events(db_session, run.id)
    assert len(terminal) == 1
    assert terminal[0].payload_json["error_code"] == "publish_error"


async def test_reviewer_exception_emits_run_failed_with_error_code(
    db_session, user_factory
) -> None:
    """Reviewer 调用异常：run.failed 终态事件带稳定 error_code=review_error（G1）。"""
    run, attempt, _, _ = await _setup_run(db_session, user_factory)
    _, draft, _ = await _make_draft(db_session, run)
    # decisions 为空：FakeReviewerGateway 抛 AssertionError，走 _abort_review 收口
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
        decisions=[],
    )
    outcome = await engine.run(
        run=run,
        attempt_id=attempt.id,
        profile=get_profile("session_analyst_v1"),
        messages=[ChatMessage(role="user", content="分析瑞幸品牌")],
    )
    assert outcome.status == RunStatus.FAILED
    types = await _event_types(db_session, run.id)
    assert types[-1] == "run.failed"
    terminal = await _terminal_events(db_session, run.id)
    assert len(terminal) == 1
    assert terminal[0].payload_json["error_code"] == "review_error"


async def test_decide_exception_emits_run_failed_with_error_code(
    db_session, user_factory
) -> None:
    """decide 系统异常：run.failed 终态事件带稳定 error_code=model_error（G1）。"""
    run, attempt, _, _ = await _setup_run(db_session, user_factory)
    # actions 为空：FakeAgentGateway 抛 AssertionError，按系统错误收口
    engine, _, _ = _make_engine(db_session, actions=[], decisions=[])
    outcome = await engine.run(
        run=run,
        attempt_id=attempt.id,
        profile=get_profile("session_analyst_v1"),
        messages=[ChatMessage(role="user", content="开始")],
    )
    assert outcome.status == RunStatus.FAILED
    types = await _event_types(db_session, run.id)
    assert types[-1] == "run.failed"
    terminal = await _terminal_events(db_session, run.id)
    assert len(terminal) == 1
    assert terminal[0].payload_json["error_code"] == "model_error"


async def test_max_invalid_actions_emits_run_failed_with_error_code(
    db_session, user_factory
) -> None:
    """连续无效动作达上限：run.failed 带 error_code=max_invalid_actions（G1）。"""
    run, attempt, _, _ = await _setup_run(db_session, user_factory)
    engine, _, _ = _make_engine(
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
    terminal = await _terminal_events(db_session, run.id)
    assert len(terminal) == 1
    assert terminal[0].event_type == "run.failed"
    assert terminal[0].payload_json["error_code"] == "max_invalid_actions"


# ---------------------------------------------------------------------------
# 9b. G1：artifact 事件接入统一 Run SSE（§15.3）
# ---------------------------------------------------------------------------


async def test_create_draft_tool_emits_artifact_draft_created_event(
    db_session, user_factory
) -> None:
    """create_draft 工具成功：tool 流程中发 artifact.draft.created（G1/P1）。

    payload 带 artifact_id/module/parent_artifact_id/status（§15.3），
    顺序在 tool.succeeded 之后、message.completed 之前。
    """
    run, attempt, _, _ = await _setup_run(db_session, user_factory)
    registry = ToolRegistry()
    registry.register(CreateDraftTool(db_session), category="artifact")
    engine, _, _ = _make_engine(
        db_session,
        actions=[
            CallTool(
                action="call_tool",
                internal_tool_name="create_draft",
                arguments={
                    "module": "insight",
                    "schema_version": "insight_board_v1",
                    "artifact_type": "insight_board_v1",
                    "business_fields": {
                        "parent_artifact_version_id": "pv-1",
                        "question": "瑞幸",
                    },
                    "payload": PAYLOAD_V1,
                    "evidence_refs": [],
                },
                rationale="创建洞察 Draft",
            ),
            Complete(action="complete", text="完成"),
        ],
        decisions=[],
        registry=registry,
    )
    outcome = await engine.run(
        run=run,
        attempt_id=attempt.id,
        profile=get_profile("session_analyst_v1"),
        messages=[ChatMessage(role="user", content="分析瑞幸")],
    )
    assert outcome.status == RunStatus.COMPLETED
    rows = (
        await db_session.scalars(
            select(AgentEvent)
            .where(AgentEvent.run_id == run.id)
            .order_by(AgentEvent.sequence)
        )
    ).all()
    types = [row.event_type for row in rows]
    assert "artifact.draft.created" in types
    assert types.index("tool.succeeded") < types.index("artifact.draft.created")
    assert types.index("artifact.draft.created") < types.index("message.completed")
    created = rows[types.index("artifact.draft.created")]
    artifact = await db_session.scalar(
        select(AgentArtifact).where(AgentArtifact.session_id == run.session_id)
    )
    assert artifact is not None
    payload = created.payload_json
    assert payload["artifact_id"] == artifact.id
    assert payload["module"] == "insight"
    assert payload["parent_artifact_id"] is None
    assert payload["status"] == "draft"
    assert payload["version"] == 1
    assert payload["run_id"] == run.id


async def test_update_draft_tool_emits_artifact_draft_updated_event(
    db_session, user_factory
) -> None:
    """update_draft 工具成功：发 artifact.draft.updated，version 为新 revision（G1/P1）。"""
    run, attempt, _, _ = await _setup_run(db_session, user_factory)
    _, draft, _ = await _make_draft(db_session, run)
    registry = ToolRegistry()
    registry.register(UpdateDraftTool(db_session), category="artifact")
    engine, _, _ = _make_engine(
        db_session,
        actions=[
            CallTool(
                action="call_tool",
                internal_tool_name="update_draft",
                arguments={
                    "draft_id": draft.id,
                    "payload": PAYLOAD_V1,
                    "evidence_refs": [],
                },
                rationale="更新洞察 Draft",
            ),
            Complete(action="complete", text="完成"),
        ],
        decisions=[],
        registry=registry,
    )
    outcome = await engine.run(
        run=run,
        attempt_id=attempt.id,
        profile=get_profile("session_analyst_v1"),
        messages=[ChatMessage(role="user", content="分析瑞幸")],
    )
    assert outcome.status == RunStatus.COMPLETED
    rows = (
        await db_session.scalars(
            select(AgentEvent)
            .where(AgentEvent.run_id == run.id)
            .order_by(AgentEvent.sequence)
        )
    ).all()
    types = [row.event_type for row in rows]
    assert "artifact.draft.updated" in types
    assert types.index("tool.succeeded") < types.index("artifact.draft.updated")
    assert types.index("artifact.draft.updated") < types.index("message.completed")
    updated = rows[types.index("artifact.draft.updated")]
    payload = updated.payload_json
    assert payload["artifact_id"] == draft.artifact_id
    assert payload["module"] == "insight"
    assert payload["status"] == "draft"
    assert payload["version"] == 2


async def test_approve_publish_emits_artifact_published_before_message_completed(
    db_session, user_factory
) -> None:
    """原子发布成功：每个发布的 Artifact 发一条 artifact.published（G1/P1）。

    顺序：review.approved → artifact.published → message.completed → run.completed；
    payload 带 artifact_id/module/parent_artifact_id/status/version（§15.3）。
    """
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
    rows = (
        await db_session.scalars(
            select(AgentEvent)
            .where(AgentEvent.run_id == run.id)
            .order_by(AgentEvent.sequence)
        )
    ).all()
    types = [row.event_type for row in rows]
    assert types.count("artifact.published") == 1
    assert types.index("review.approved") < types.index("artifact.published")
    assert types.index("artifact.published") < types.index("message.completed")
    assert types[-1] == "run.completed"
    published = rows[types.index("artifact.published")]
    payload = published.payload_json
    assert payload["artifact_id"] == draft.artifact_id
    assert payload["module"] == "insight"
    assert payload["parent_artifact_id"] is None
    assert payload["status"] == "published"
    assert payload["version"] == 1
    terminal = await _terminal_events(db_session, run.id)
    assert len(terminal) == 1


async def _purge_committed_review_run(
    user_id: str, session_id: str, run_id: str
) -> None:
    """删除 review 路径跨会话测试提交的行（含 review/artifact 链与内部 Run）。"""
    async with db_engine.begin() as conn:
        run_ids = select(AgentRun.id).where(AgentRun.session_id == session_id)
        batch_ids = select(ArtifactReviewBatch.id).where(
            ArtifactReviewBatch.parent_run_id == run_id
        )
        item_ids = select(ArtifactReviewItem.id).where(
            ArtifactReviewItem.batch_id.in_(batch_ids)
        )
        await conn.execute(
            delete(ArtifactReviewAttempt).where(
                ArtifactReviewAttempt.review_item_id.in_(item_ids)
            )
        )
        await conn.execute(
            delete(ArtifactReviewItem).where(ArtifactReviewItem.batch_id.in_(batch_ids))
        )
        await conn.execute(
            delete(ArtifactReviewBatch).where(
                ArtifactReviewBatch.parent_run_id == run_id
            )
        )
        await conn.execute(
            delete(ArtifactEvent).where(ArtifactEvent.session_id == session_id)
        )
        draft_ids = select(ArtifactDraft.id).where(ArtifactDraft.session_id == session_id)
        await conn.execute(
            delete(ArtifactDraftRevision).where(
                ArtifactDraftRevision.draft_id.in_(draft_ids)
            )
        )
        await conn.execute(
            delete(ArtifactDraft).where(ArtifactDraft.session_id == session_id)
        )
        await conn.execute(
            delete(AgentArtifact).where(AgentArtifact.session_id == session_id)
        )
        await conn.execute(delete(AgentEvent).where(AgentEvent.run_id.in_(run_ids)))
        await conn.execute(
            delete(AgentMessage).where(AgentMessage.session_id == session_id)
        )
        await conn.execute(delete(AgentStep).where(AgentStep.run_id.in_(run_ids)))
        await conn.execute(
            delete(AgentRunAttempt).where(AgentRunAttempt.run_id.in_(run_ids))
        )
        # agent_runs.parent_run_id 自引用：先删 Reviewer 内部子 Run，再删父 Run
        # （InnoDB 单语句多行删除自引用 RESTRICT 会失败）。
        await conn.execute(delete(AgentRun).where(AgentRun.parent_run_id == run_id))
        await conn.execute(delete(AgentRun).where(AgentRun.session_id == session_id))
        await conn.execute(delete(AgentSession).where(AgentSession.id == session_id))
        await conn.execute(delete(User).where(User.id == user_id))


async def test_live_stream_reject_ends_with_run_failed() -> None:
    """跨会话在线消费（reader 独立会话）：reject 路径的 SSE 流以 run.failed
    终态事件收口——流必须结束，不能让前端停在中间态（§5.8/G1 P0）。"""
    user_id, session_id, run_id = await _create_committed_agent_run()
    broker = AgentEventBroker()
    try:
        async with SessionFactory() as writer:
            repo = AgentRunRepository(writer)
            attempt = await repo.begin_attempt(run_id)
            assert await repo.claim_lease(run_id, "worker", 300)
            run = await writer.get(AgentRun, run_id)
            assert run is not None
            _, draft, _ = await ArtifactService(writer).create_or_get_draft(
                session_id=session_id,
                user_id=user_id,
                run_id=run_id,
                module="insight",
                business_fields={"parent_artifact_version_id": "pv-1", "question": "瑞幸"},
                schema_version="insight_board_v1",
                payload=PAYLOAD_V1,
                evidence_refs=None,
                artifact_type="insight_board_v1",
            )
            await writer.commit()
            engine = AgentEngine(
                writer,
                gateway=FakeAgentGateway(
                    [
                        SubmitReview(
                            action="submit_review",
                            artifact_draft_ids=(draft.id,),
                            completion_text="分析",
                            summary="瑞幸品牌分析",
                        )
                    ]
                ),
                registry=ToolRegistry(),
                events=AgentEventStream(writer, broker),
                reviewer=ReviewerDriver(
                    writer,
                    FakeReviewerGateway(
                        [
                            ReviewDecision(
                                decision="reject",
                                issues=[
                                    ReviewIssue(code="untrusted", message="数字无法追溯")
                                ],
                            )
                        ]
                    ),
                    worker_id="worker",
                ),
                worker_id="worker",
            )

            received: list[str] = []

            async def consume() -> None:
                async with SessionFactory() as reader:
                    stream = AgentEventStream(reader, broker)
                    async for event in stream.stream(run_id, user_id, last_event_id=0):
                        received.append(event.event_type)

            consumer = asyncio.create_task(consume())
            await asyncio.sleep(0.05)  # reader 完成重放并进入 broker 等待
            outcome = await engine.run(
                run=run,
                attempt_id=attempt.id,
                profile=get_profile("session_analyst_v1"),
                messages=[ChatMessage(role="user", content="分析瑞幸品牌")],
            )
            assert outcome.status == RunStatus.FAILED
            # reject 修复前：流永远等不到终态事件，这里会超时失败
            await asyncio.wait_for(consumer, timeout=5)
    finally:
        await _purge_committed_review_run(user_id, session_id, run_id)

    assert received[-1] == "run.failed"
    assert "review.rejected" in received
    assert received.index("review.rejected") < received.index("run.failed")
    assert sum(1 for t in received if t in _TERMINAL_TYPES) == 1
