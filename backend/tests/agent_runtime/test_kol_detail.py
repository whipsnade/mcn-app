"""kol_detail_v1 轻量 Run 服务集成测试（设计 §13.2 / §8.1 / §12.1 / Task 17）。

用脚本化 fake 网关 + 真实 Task 14 引擎 + 真实 builder / Artifact 服务驱动
``KolDetailRunService.create``，覆盖：
1. 缓存命中：同一 (user, session, platform, kol_uid) 24h 内零模型/MCP 调用；
2. 跨 Session 隔离：不同 Session 不命中缓存；
3. 缓存过期：注入时钟后允许刷新（重新创建 Run 让模型补查）；
4. 轻量 Run 形状：run_kind=user / visibility=user / profile=kol_detail_v1；
   独立并发车道（活动 session_analyst_v1 并行不阻塞；同 (platform, kol_uid)
   存在活动 kol-detail Run 时幂等返回现有 Run）；
5. kol_detail_v2 契约：builder 输出被 Task 10 Schema 接受，URL 仅 http/https，
   缺 homepage_url / 帖 url 时披露 limitation。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select

from app.agent_artifacts.builders.kol_detail import build_kol_detail_draft
from app.agent_artifacts.models import (
    AgentArtifact,
    AgentArtifactVersion,
    ArtifactDraft,
    KolDetailCache,
)
from app.agent_artifacts.payloads.kol_detail import KolDetailV2
from app.agent_runtime.engine import AgentEngine
from app.agent_runtime.evidence import EvidenceWriter
from app.agent_runtime.events import AgentEventBroker, AgentEventStream
from app.agent_runtime.kol_detail import (
    KolDetailRunFailed,
    KolDetailRunService,
    build_kol_detail_prompt_snapshot,
    kol_detail_trigger_content,
)
from app.agent_runtime.models import (
    AgentEvent,
    AgentMessage,
    AgentRun,
    AgentRunAttempt,
    AgentSession,
    AgentStep,
    AgentToolCall,
    EvidenceItem,
)
from app.agent_runtime.profiles import get_profile
from app.agent_runtime.repository import AgentRunRepository, utc_now
from app.agent_runtime.reviewer import ReviewDecision, ReviewerDriver
from app.agent_runtime.schemas import CallTool, SubmitReview
from app.agent_runtime.state import RunStatus
from app.agent_runtime.thinking import AgentEventThinkingSink
from app.agent_runtime.tools.artifacts import CreateDraftTool
from app.agent_runtime.tools.contracts import ToolResult
from app.agent_runtime.tools.registry import ToolRegistry
from app.agent_runtime.transcript import RunTranscriptLoader

PLATFORM = "xiaohongshu"
KOL_UID = "k1"
T0 = datetime(2026, 1, 1, 12, 0, 0)

DETAIL: dict[str, Any] = {
    "identity": {
        "nickname": "达人K",
        "avatar_url": "https://example.com/avatar.png",
        "homepage_url": "https://example.com/k1",
        "bio": "美食博主",
        "verification": True,
        "region": "上海",
    },
    "metrics": {
        "followers": 100000,
        "following": 100,
        "posts": 500,
        "likes": 50000,
        "active_followers": 50000,
        "active_follower_rate": 50.0,
        "growth_rate": 2.0,
        "engagement_total": 5000,
        "avg_engagement": 50.0,
    },
    "audience": {
        "gender_distribution": [
            {"key": "f", "label": "女", "value": 60000, "share": 0.6}
        ],
        "age_distribution": [
            {"key": "25-34", "label": "25-34", "value": 40000, "share": 0.4}
        ],
        "region_distribution": [
            {"key": "sh", "label": "上海", "value": 30000, "share": 0.3}
        ],
        "interest_distribution": [
            {"key": "coffee", "label": "咖啡", "value": 20000, "share": 0.2}
        ],
    },
    "trend": [
        {"date": "2026-01-01", "followers": 100000, "engagement": 500, "posts": 5}
    ],
    "latest_posts": [
        {
            "post_id": "p1",
            "title": "热帖",
            "url": "https://example.com/p1",
            "published_at": "2026-01-01T12:00:00",
            "likes": 100,
            "comments": 10,
            "shares": 5,
            "engagement": 115,
        }
    ],
}


def _cache_state(*, fetched: datetime = T0) -> dict[str, Any]:
    return {
        "hit": False,
        "fetched_at": fetched.isoformat(),
        "expires_at": (fetched + timedelta(hours=24)).isoformat(),
    }


# ---------------------------------------------------------------------------
# fake 网关 / 工具
# ---------------------------------------------------------------------------


class FetchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: str = ""
    kol_uid: str = ""


class FakeKolDetailFetchTool:
    """kol_detail 分类下的抓取工具：零积分，返回已抓取 Evidence 的 id。"""

    name = "kol_detail_fetch"
    input_model = FetchArgs
    points_cost = 0
    external_side_effect = False

    def __init__(self, evidence_id: str) -> None:
        self._evidence_id = evidence_id

    async def execute(self, context: Any, arguments: BaseModel) -> ToolResult:
        return ToolResult(
            status="success",
            safe_summary="kol detail fetched",
            evidence_id=self._evidence_id,
        )


class KolDetailFakeGateway:
    """脚本化动作网关；支持可调用动作（工厂）在运行时解析 draft id。

    ``interleave``：在首次 submit_review 被分发前注入一次并发 ``create()``，
    模拟 TOCTOU 窗口（第二个 create 撞上正在进行的 Run）。
    """

    def __init__(self, actions: list[Any]) -> None:
        self.actions = list(actions)
        self.calls: list[dict[str, Any]] = []
        self.interleave = None
        self.interleave_result = None

    async def decide(self, *, run, attempt_id, profile, messages, thinking_sink=None, **kwargs):
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
        if not self.actions:
            raise AssertionError("fake gateway exhausted")
        action = self.actions.pop(0)
        if callable(action):
            action = await action(run)
        if (
            self.interleave is not None
            and self.interleave_result is None
            and isinstance(action, SubmitReview)
        ):
            self.interleave_result = await self.interleave()
        return action


class ApprovingReviewerGateway:
    """每次都 approve 的 Reviewer 网关，供多条 run 复用（不消费决策）。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def decide(self, *, run, attempt_id, profile, messages, thinking_sink=None, **kwargs):
        self.calls.append({"run_id": run.id, **kwargs})
        return ReviewDecision(decision="approve")


# ---------------------------------------------------------------------------
# 装配辅助
# ---------------------------------------------------------------------------


async def _make_session(db, user_id: str) -> AgentSession:
    now = utc_now()
    session = AgentSession(
        id=str(uuid4()),
        user_id=user_id,
        title="达人详情会话",
        status="active",
        created_at=now,
        updated_at=now,
    )
    db.add(session)
    await db.flush()
    return session


async def _make_evidence(db, user_id: str, session_id: str):
    """创建持有所需 raw payload 的不可变 Evidence（附 step/tool_call 链条）。"""
    now = utc_now()
    run = AgentRun(
        id=str(uuid4()),
        session_id=session_id,
        user_id=user_id,
        run_kind="internal",
        visibility="internal",
        profile_name="evidence_chain",
        profile_version="v1",
        model="test-model",
        status="running",
        started_at=now,
    )
    db.add(run)
    await db.flush()
    attempt = AgentRunAttempt(id=str(uuid4()), run_id=run.id, attempt=1, started_at=now)
    db.add(attempt)
    await db.flush()
    step = AgentStep(
        id=str(uuid4()),
        run_id=run.id,
        attempt_id=attempt.id,
        sequence=1,
        step_type="tool_call",
        status="running",
        created_at=now,
    )
    db.add(step)
    await db.flush()
    call = AgentToolCall(
        id=str(uuid4()),
        run_id=run.id,
        step_id=step.id,
        logical_call_id=f"src-detail-{uuid4().hex[:8]}",
        service="mcp",
        internal_tool_name="kol_detail_fetch",
        arguments_json={},
        arguments_hash="h",
        status="settled",
        points_reserved=10,
        points_settled=10,
        started_at=now,
        completed_at=now,
    )
    db.add(call)
    await db.flush()
    evidence = await EvidenceWriter(db).write(
        session_id=session_id,
        run_id=run.id,
        tool_call_id=call.id,
        source_type="mcp",
        source_name="kol detail",
        scope_json=None,
        period_json=None,
        raw_payload=DETAIL,
    )
    return evidence


def _make_actions(db, evidence, cache_state: dict[str, Any]) -> list[Any]:
    """脚本化 kol_detail_v1 动作：抓取 → 创建 Draft（真实 builder）→ 提交复核。"""
    build = build_kol_detail_draft(
        platform=PLATFORM,
        kol_uid=KOL_UID,
        selection_artifact_id=None,
        selection_version=None,
        detail=DETAIL,
        evidence_id=evidence.id,
        cache_state=cache_state,
    )
    create_args = {
        "module": build.module,
        "schema_version": build.schema_version,
        "artifact_type": build.artifact_type,
        "business_fields": build.business_fields,
        "payload": build.payload,
        "evidence_refs": build.evidence_refs,
    }

    async def submit(run):
        draft = await db.scalar(
            select(ArtifactDraft).where(ArtifactDraft.owner_run_id == run.id)
        )
        assert draft is not None
        return SubmitReview(
            action="submit_review",
            artifact_draft_ids=(draft.id,),
            completion_text="达人详情已完成",
            summary="达人详情",
        )

    return [
        CallTool(
            action="call_tool",
            internal_tool_name="kol_detail_fetch",
            arguments={"platform": PLATFORM, "kol_uid": KOL_UID},
            rationale="抓取达人详情",
        ),
        CallTool(
            action="call_tool",
            internal_tool_name="create_draft",
            arguments=create_args,
            rationale="创建达人详情 Draft",
        ),
        submit,
    ]


def _make_service(db, *, actions: list[Any], evidence, now_fn, worker: str = "worker"):
    gateway = KolDetailFakeGateway(actions)
    registry = ToolRegistry()
    registry.register(FakeKolDetailFetchTool(evidence.id), category="kol_detail")
    registry.register(CreateDraftTool(db), category="artifact")
    reviewer_gateway = ApprovingReviewerGateway()
    broker = AgentEventBroker()
    events = AgentEventStream(db, broker)
    reviewer = ReviewerDriver(db, reviewer_gateway, worker_id=worker)
    engine = AgentEngine(
        db,
        gateway=gateway,
        registry=registry,
        events=events,
        reviewer=reviewer,
        worker_id=worker,
    )
    service = KolDetailRunService(
        db,
        engine=engine,
        worker_id=worker,
        cache_ttl_hours=24,
        model="test-model",
        now_fn=now_fn,
    )
    return gateway, service


# ---------------------------------------------------------------------------
# 1. 缓存命中
# ---------------------------------------------------------------------------


async def test_cache_hit_serves_without_new_model_call(db_session, user_factory) -> None:
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    evidence = await _make_evidence(db_session, user.id, session.id)
    gateway, service = _make_service(
        db_session,
        actions=_make_actions(db_session, evidence, _cache_state()),
        evidence=evidence,
        now_fn=lambda: T0,
    )

    # 第一次：缓存未命中 → 创建轻量 Run，模型抓取 → 发布 → 回填缓存。
    summary1 = await service.create(user.id, session.id, PLATFORM, KOL_UID)
    assert summary1.cached is False
    assert summary1.run_id is not None
    assert summary1.detail is not None
    assert summary1.detail["data"]["cache"]["hit"] is False
    calls_after_first = len(gateway.calls)
    assert calls_after_first == 3  # fetch + create_draft + submit_review

    # 轻量 Run 形状：run_kind=user / visibility=user / profile=kol_detail_v1。
    run_row = await db_session.get(AgentRun, summary1.run_id)
    assert run_row is not None
    assert run_row.run_kind == "user"
    assert run_row.visibility == "user"
    assert run_row.profile_name == "kol_detail_v1"
    assert run_row.status == RunStatus.COMPLETED

    # kol_detail_v2 已发布。
    version = await db_session.scalar(
        select(AgentArtifactVersion).where(
            AgentArtifactVersion.source_run_id == summary1.run_id
        )
    )
    assert version is not None
    assert version.schema_version == "kol_detail_v2"

    # 缓存已回填。
    cache_row = await db_session.scalar(
        select(KolDetailCache).where(KolDetailCache.session_id == session.id)
    )
    assert cache_row is not None
    assert cache_row.platform == PLATFORM
    assert cache_row.kol_uid == KOL_UID

    # 第二次：缓存命中 → 零模型/MCP 调用，detail 带 cache.hit=true 与时间戳。
    summary2 = await service.create(user.id, session.id, PLATFORM, KOL_UID)
    assert summary2.cached is True
    assert summary2.run_id is None
    assert summary2.detail is not None
    assert summary2.detail["data"]["cache"]["hit"] is True
    assert summary2.detail["data"]["cache"]["fetched_at"]
    assert summary2.detail["data"]["cache"]["expires_at"]
    assert len(gateway.calls) == calls_after_first  # 无新模型调用
    assert summary2.detail["data"]["latest_posts"]  # 缓存保留热帖


async def test_fresh_run_receives_thinking_sink(db_session, user_factory) -> None:
    """kol_detail Run 是用户可见 Run：引擎注入 AgentEventThinkingSink，
    主 Agent 真实 thinking 才能实时 SSE（§5.8/§10.5）。"""
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    evidence = await _make_evidence(db_session, user.id, session.id)
    gateway, service = _make_service(
        db_session,
        actions=_make_actions(db_session, evidence, _cache_state()),
        evidence=evidence,
        now_fn=lambda: T0,
    )

    summary = await service.create(user.id, session.id, PLATFORM, KOL_UID)

    assert summary.cached is False
    assert gateway.calls
    assert all(
        isinstance(call["thinking_sink"], AgentEventThinkingSink)
        for call in gateway.calls
    )


async def test_fresh_run_emits_artifact_events_and_terminal_event(
    db_session, user_factory
) -> None:
    """kol_detail Run 的产物事件同样接入统一 Run SSE（G1/§15.3）：

    artifact.draft.created → artifact.published（message.completed 之前）→
    run.completed 终态事件收尾，前端据此实时刷新右侧 BI。
    """
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    evidence = await _make_evidence(db_session, user.id, session.id)
    gateway, service = _make_service(
        db_session,
        actions=_make_actions(db_session, evidence, _cache_state()),
        evidence=evidence,
        now_fn=lambda: T0,
    )

    summary = await service.create(user.id, session.id, PLATFORM, KOL_UID)

    assert summary.cached is False
    assert summary.run_id is not None
    rows = (
        await db_session.scalars(
            select(AgentEvent)
            .where(AgentEvent.run_id == summary.run_id)
            .order_by(AgentEvent.sequence)
        )
    ).all()
    types = [row.event_type for row in rows]
    assert "artifact.draft.created" in types
    assert types.count("artifact.published") == 1
    assert types.index("artifact.draft.created") < types.index("artifact.published")
    assert types.index("artifact.published") < types.index("message.completed")
    assert types[-1] == "run.completed"
    published = rows[types.index("artifact.published")]
    assert published.payload_json["artifact_id"] == summary.artifact_id
    assert published.payload_json["module"] == "kol-detail"
    assert published.payload_json["status"] == "published"
    assert published.payload_json["version"] == 1
    created = rows[types.index("artifact.draft.created")]
    assert created.payload_json["artifact_id"] == summary.artifact_id
    assert created.payload_json["module"] == "kol-detail"
    assert created.payload_json["status"] == "draft"


# ---------------------------------------------------------------------------
# 2. 跨 Session 隔离
# ---------------------------------------------------------------------------


async def test_cross_session_not_served_from_cache(db_session, user_factory) -> None:
    user = await user_factory()
    session_a = await _make_session(db_session, user.id)
    session_b = await _make_session(db_session, user.id)
    evidence_a = await _make_evidence(db_session, user.id, session_a.id)
    evidence_b = await _make_evidence(db_session, user.id, session_b.id)
    gateway, service = _make_service(
        db_session,
        actions=_make_actions(db_session, evidence_a, _cache_state()),
        evidence=evidence_a,
        now_fn=lambda: T0,
    )

    summary_a = await service.create(user.id, session_a.id, PLATFORM, KOL_UID)
    assert summary_a.cached is False
    calls_after_a = len(gateway.calls)

    # 同一 (platform, kol_uid) 在另一个 Session：不命中 session A 的缓存。
    gateway.actions = _make_actions(db_session, evidence_b, _cache_state())
    summary_b = await service.create(user.id, session_b.id, PLATFORM, KOL_UID)
    assert summary_b.cached is False
    assert summary_b.run_id is not None
    assert len(gateway.calls) > calls_after_a  # 触发了新的模型抓取


# ---------------------------------------------------------------------------
# 3. 缓存过期 → 允许刷新
# ---------------------------------------------------------------------------


async def test_cache_expiry_allows_refresh(db_session, user_factory) -> None:
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    evidence = await _make_evidence(db_session, user.id, session.id)
    gateway, service = _make_service(
        db_session,
        actions=_make_actions(db_session, evidence, _cache_state()),
        evidence=evidence,
        now_fn=lambda: T0,
    )

    summary1 = await service.create(user.id, session.id, PLATFORM, KOL_UID)
    assert summary1.cached is False
    calls_before = len(gateway.calls)

    # 注入时钟越过 expires_at → 缓存过期，模型可重新抓取（创建新 Run）。
    service.now_fn = lambda: T0 + timedelta(hours=25)
    gateway.actions = _make_actions(db_session, evidence, _cache_state())
    summary2 = await service.create(user.id, session.id, PLATFORM, KOL_UID)
    assert summary2.cached is False
    assert summary2.run_id is not None
    assert summary2.run_id != summary1.run_id
    assert len(gateway.calls) > calls_before


# ---------------------------------------------------------------------------
# 4. 轻量 Run 形状与并发车道
# ---------------------------------------------------------------------------


async def test_parallel_lane_with_active_session_analyst(db_session, user_factory) -> None:
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    now = utc_now()
    # 同一 Session 已有活动 session_analyst_v1 Run（不持有 kol-detail artifact）。
    sa_run = AgentRun(
        id=str(uuid4()),
        session_id=session.id,
        user_id=user.id,
        run_kind="user",
        visibility="user",
        profile_name="session_analyst_v1",
        profile_version="v1",
        model="test-model",
        status="running",
        started_at=now,
    )
    db_session.add(sa_run)
    await db_session.flush()

    evidence = await _make_evidence(db_session, user.id, session.id)
    gateway, service = _make_service(
        db_session,
        actions=_make_actions(db_session, evidence, _cache_state()),
        evidence=evidence,
        now_fn=lambda: T0,
    )
    summary = await service.create(user.id, session.id, PLATFORM, KOL_UID)
    assert summary.cached is False
    # 独立车道：新建 kol-detail Run，不被活动 session_analyst Run 阻塞。
    assert summary.run_id != sa_run.id
    run_row = await db_session.get(AgentRun, summary.run_id)
    assert run_row.profile_name == "kol_detail_v1"
    assert run_row.status == RunStatus.COMPLETED


async def test_same_kol_detail_active_run_is_idempotent(db_session, user_factory) -> None:
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    now = utc_now()
    # 已存在的活动 kol-detail Run 持有 kol-detail artifact 的 working head。
    active_run = AgentRun(
        id=str(uuid4()),
        session_id=session.id,
        user_id=user.id,
        run_kind="user",
        visibility="user",
        profile_name="kol_detail_v1",
        profile_version="v1",
        model="test-model",
        status="running",
        started_at=now,
    )
    db_session.add(active_run)
    await db_session.flush()
    artifact = AgentArtifact(
        id=str(uuid4()),
        session_id=session.id,
        user_id=user.id,
        module="kol-detail",
        artifact_type="kol_detail_v2",
        artifact_key="kol-detail:xiaohongshu:k1",
        status="draft",
        latest_version=0,
        activity_sequence=0,
        created_at=now,
        updated_at=now,
    )
    db_session.add(artifact)
    await db_session.flush()
    draft = ArtifactDraft(
        id=str(uuid4()),
        artifact_id=artifact.id,
        session_id=session.id,
        owner_run_id=active_run.id,
        current_revision=0,
        status="drafting",
        review_count=0,
        revision_count=0,
        updated_at=now,
    )
    db_session.add(draft)
    await db_session.flush()

    service = KolDetailRunService(
        db_session,
        engine=None,
        cache_ttl_hours=24,
        model="test-model",
        now_fn=lambda: T0,
    )
    summary = await service.create(user.id, session.id, PLATFORM, KOL_UID)
    assert summary.cached is False
    # 幂等返回现有 Run，不创建第二个。
    assert summary.run_id == active_run.id
    count = await db_session.scalar(
        select(func.count(AgentRun.id)).where(
            AgentRun.session_id == session.id,
            AgentRun.profile_name == "kol_detail_v1",
        )
    )
    assert count == 1


async def test_concurrent_create_interleave_only_one_run(db_session, user_factory) -> None:
    """TOCTOU 竞态（Fix 1）：首个 Run 进行中并发 create → 幂等返回同一 Run。"""
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    evidence = await _make_evidence(db_session, user.id, session.id)
    gateway, service = _make_service(
        db_session,
        actions=_make_actions(db_session, evidence, _cache_state()),
        evidence=evidence,
        now_fn=lambda: T0,
    )

    async def interleave():
        return await service.create(user.id, session.id, PLATFORM, KOL_UID)

    gateway.interleave = interleave

    summary = await service.create(user.id, session.id, PLATFORM, KOL_UID)
    assert summary.cached is False
    # 并发 create 在首个 Run 提交复核时触发：幂等返回同一活动 Run，不创建第二个。
    assert gateway.interleave_result is not None
    assert gateway.interleave_result.run_id == summary.run_id
    assert gateway.interleave_result.cached is False
    count = await db_session.scalar(
        select(func.count(AgentRun.id)).where(
            AgentRun.session_id == session.id,
            AgentRun.profile_name == "kol_detail_v1",
        )
    )
    assert count == 1
    # 缓存回填成功（不 500）。
    cache_row = await db_session.scalar(
        select(KolDetailCache).where(KolDetailCache.session_id == session.id)
    )
    assert cache_row is not None


async def test_paused_owner_does_not_block_fresh_run(db_session, user_factory) -> None:
    """paused/终态 owner（Fix 2）：不再阻塞，释放 working head 让新 Run 接管。"""
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    now = utc_now()
    paused_run = AgentRun(
        id=str(uuid4()),
        session_id=session.id,
        user_id=user.id,
        run_kind="user",
        visibility="user",
        profile_name="kol_detail_v1",
        profile_version="v1",
        model="test-model",
        status="paused",
        started_at=now,
    )
    db_session.add(paused_run)
    await db_session.flush()
    artifact = AgentArtifact(
        id=str(uuid4()),
        session_id=session.id,
        user_id=user.id,
        module="kol-detail",
        artifact_type="kol_detail_v2",
        artifact_key="kol-detail:xiaohongshu:k1",
        status="draft",
        latest_version=0,
        activity_sequence=0,
        created_at=now,
        updated_at=now,
    )
    db_session.add(artifact)
    await db_session.flush()
    draft = ArtifactDraft(
        id=str(uuid4()),
        artifact_id=artifact.id,
        session_id=session.id,
        owner_run_id=paused_run.id,
        current_revision=0,
        status="drafting",
        review_count=0,
        revision_count=0,
        updated_at=now,
    )
    db_session.add(draft)
    await db_session.flush()

    evidence = await _make_evidence(db_session, user.id, session.id)
    gateway, service = _make_service(
        db_session,
        actions=_make_actions(db_session, evidence, _cache_state()),
        evidence=evidence,
        now_fn=lambda: T0,
    )
    summary = await service.create(user.id, session.id, PLATFORM, KOL_UID)
    assert summary.cached is False
    assert summary.run_id is not None
    assert summary.run_id != paused_run.id  # 新 Run，不被暂停的 owner 阻塞
    # 新 Run 已接管 working head 并发布；发布后 head 复位（owner=None），
    # 暂停的 owner 不再持有。
    fresh_draft = await db_session.scalar(
        select(ArtifactDraft).where(ArtifactDraft.artifact_id == artifact.id)
    )
    assert fresh_draft is not None
    assert fresh_draft.owner_run_id is None
    version = await db_session.scalar(
        select(AgentArtifactVersion).where(
            AgentArtifactVersion.source_run_id == summary.run_id
        )
    )
    assert version is not None
    # 缓存回填成功。
    cache_row = await db_session.scalar(
        select(KolDetailCache).where(KolDetailCache.session_id == session.id)
    )
    assert cache_row is not None


async def test_corrupt_cache_evicted_and_refreshed(db_session, user_factory) -> None:
    """损坏的缓存 payload：驱逐并刷新，而不是对用户 500（Fix minor）。"""
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    evidence = await _make_evidence(db_session, user.id, session.id)
    gateway, service = _make_service(
        db_session,
        actions=_make_actions(db_session, evidence, _cache_state()),
        evidence=evidence,
        now_fn=lambda: T0,
    )
    summary1 = await service.create(user.id, session.id, PLATFORM, KOL_UID)
    assert summary1.cached is False

    cache_row = await db_session.scalar(
        select(KolDetailCache).where(KolDetailCache.session_id == session.id)
    )
    assert cache_row is not None
    # 损坏 payload（缺必需字段 → Schema 校验失败）。
    cache_row.payload_json = {
        "schema_version": "kol_detail_v2",
        "data": {"cache": {"hit": False}},
    }
    await db_session.flush()

    gateway.actions = _make_actions(db_session, evidence, _cache_state())
    summary2 = await service.create(user.id, session.id, PLATFORM, KOL_UID)
    assert summary2.cached is False
    assert summary2.run_id is not None
    assert summary2.run_id != summary1.run_id
    # 旧缓存行已被驱逐，只留下新回填的一行。
    rows = (
        await db_session.scalars(
            select(KolDetailCache).where(KolDetailCache.session_id == session.id)
        )
    ).all()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# 5. kol_detail_v2 契约（builder 输出被 Task 10 Schema 接受）
# ---------------------------------------------------------------------------


def test_builder_emits_schema_valid_complete_payload() -> None:
    build = build_kol_detail_draft(
        platform=PLATFORM,
        kol_uid=KOL_UID,
        selection_artifact_id=None,
        selection_version=None,
        detail=DETAIL,
        evidence_id="ev-1",
        cache_state=_cache_state(),
    )
    payload = build.payload
    KolDetailV2.model_validate(payload)  # Task 10 Schema 接受
    assert build.module == "kol-detail"
    assert build.business_fields == {"platform": PLATFORM, "kol_uid": KOL_UID}
    assert payload["data_status"] == "complete"
    assert payload["data"]["cache"]["hit"] is False
    assert len(payload["data"]["latest_posts"]) <= 5
    # URL 仅 http/https。
    assert payload["data"]["identity"]["homepage_url"].startswith("https://")
    assert all(post["url"].startswith("https://") for post in payload["data"]["latest_posts"])
    # narrative 的 supporting_paths 都能在 data 中解析。
    assert payload["narrative"]["content_strengths"][0]["supporting_paths"]
    # lineage：data 下每个非空数值都有 Evidence 引用。
    assert any(r["artifact_path"] == "/data/metrics/followers" for r in build.evidence_refs)
    assert any(
        r["artifact_path"] == "/data/latest_posts/0/engagement" for r in build.evidence_refs
    )


def test_builder_discloses_missing_homepage_url() -> None:
    detail = dict(DETAIL)
    detail["identity"] = dict(DETAIL["identity"])
    detail["identity"].pop("homepage_url")
    build = build_kol_detail_draft(
        platform=PLATFORM,
        kol_uid=KOL_UID,
        detail=detail,
        evidence_id="ev-1",
        cache_state=_cache_state(),
    )
    payload = build.payload
    assert payload["data"]["identity"]["homepage_url"] is None  # 不伪造链接
    assert payload["data_status"] == "restricted"
    codes = {lim["code"] for lim in payload["limitations"]}
    assert "homepage_url_missing" in codes
    KolDetailV2.model_validate(payload)


def test_builder_discloses_missing_post_url() -> None:
    detail = dict(DETAIL)
    posts = [dict(post) for post in DETAIL["latest_posts"]]
    posts[0].pop("url")
    detail["latest_posts"] = posts
    build = build_kol_detail_draft(
        platform=PLATFORM,
        kol_uid=KOL_UID,
        detail=detail,
        evidence_id="ev-1",
        cache_state=_cache_state(),
    )
    payload = build.payload
    assert payload["data"]["latest_posts"][0]["url"] is None
    assert payload["data_status"] == "restricted"
    codes = {lim["code"] for lim in payload["limitations"]}
    assert "post_url_missing" in codes
    KolDetailV2.model_validate(payload)


def test_builder_rejects_non_http_url_scheme() -> None:
    detail = dict(DETAIL)
    detail["identity"] = dict(DETAIL["identity"])
    detail["identity"]["homepage_url"] = "javascript:alert(1)"
    build = build_kol_detail_draft(
        platform=PLATFORM,
        kol_uid=KOL_UID,
        detail=detail,
        evidence_id="ev-1",
        cache_state=_cache_state(),
    )
    assert build.payload["data"]["identity"]["homepage_url"] is None
    assert "homepage_url_missing" in {
        lim["code"] for lim in build.payload["limitations"]
    }
    KolDetailV2.model_validate(build.payload)


def test_builder_discloses_partial_audience_sections() -> None:
    """部分受众分布缺失：partial 披露，不把缺失当完整（Fix minor）。"""
    detail = dict(DETAIL)
    detail["audience"] = dict(DETAIL["audience"])
    detail["audience"]["gender_distribution"] = []
    detail["audience"]["age_distribution"] = []
    build = build_kol_detail_draft(
        platform=PLATFORM,
        kol_uid=KOL_UID,
        detail=detail,
        evidence_id="ev-1",
        cache_state=_cache_state(),
    )
    payload = build.payload
    assert payload["data_status"] == "restricted"
    codes = {lim["code"] for lim in payload["limitations"]}
    assert "audience_partial" in codes
    assert payload["availability"]["audience"]["status"] == "partial"
    KolDetailV2.model_validate(payload)


def test_builder_rejects_empty_host_http_url() -> None:
    """``http://``（空 host）视为缺失，不产出伪造链接（Fix minor）。"""
    detail = dict(DETAIL)
    detail["identity"] = dict(DETAIL["identity"])
    detail["identity"]["homepage_url"] = "http://"
    build = build_kol_detail_draft(
        platform=PLATFORM,
        kol_uid=KOL_UID,
        detail=detail,
        evidence_id="ev-1",
        cache_state=_cache_state(),
    )
    assert build.payload["data"]["identity"]["homepage_url"] is None
    assert "homepage_url_missing" in {
        lim["code"] for lim in build.payload["limitations"]
    }
    KolDetailV2.model_validate(build.payload)


# ---------------------------------------------------------------------------
# 6. 请求协调（G3）：prompt_snapshot 触发上下文 / 失败收口 / 崩溃接管锚点
# ---------------------------------------------------------------------------


async def test_create_persists_kol_detail_prompt_snapshot(db_session, user_factory) -> None:
    """kol_detail Run 创建时持久化 platform/kol_uid 触发上下文（G3 恢复锚点）。"""
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    evidence = await _make_evidence(db_session, user.id, session.id)
    gateway, service = _make_service(
        db_session,
        actions=_make_actions(db_session, evidence, _cache_state()),
        evidence=evidence,
        now_fn=lambda: T0,
    )

    summary = await service.create(user.id, session.id, PLATFORM, KOL_UID)

    assert summary.cached is False
    run_row = await db_session.get(AgentRun, summary.run_id)
    assert run_row is not None
    snapshot = run_row.prompt_snapshot_json or {}
    trigger = snapshot.get("kol_detail") or {}
    assert trigger.get("platform") == PLATFORM
    assert trigger.get("kol_uid") == KOL_UID


async def test_engine_failure_commits_terminal_state_and_releases_working_head(
    db_session, user_factory
) -> None:
    """引擎失败收口（G3 协调行已提交后）：Run 落 failed 终态、working head 释放、
    Artifact 身份保留——下一次 create 直接接管，不撞 artifact_busy。"""
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    evidence = await _make_evidence(db_session, user.id, session.id)
    gateway, service = _make_service(
        db_session,
        actions=[],  # decide 立即耗尽抛错 → 引擎 model_error 收口
        evidence=evidence,
        now_fn=lambda: T0,
    )

    with pytest.raises(KolDetailRunFailed):
        await service.create(user.id, session.id, PLATFORM, KOL_UID)

    run_row = await db_session.scalar(
        select(AgentRun).where(
            AgentRun.session_id == session.id,
            AgentRun.profile_name == "kol_detail_v1",
        )
    )
    assert run_row is not None
    assert run_row.status == RunStatus.FAILED
    assert run_row.lease_owner is None
    artifact = await db_session.scalar(
        select(AgentArtifact).where(AgentArtifact.session_id == session.id)
    )
    assert artifact is not None
    draft = await db_session.scalar(
        select(ArtifactDraft).where(ArtifactDraft.artifact_id == artifact.id)
    )
    assert draft is not None
    assert draft.owner_run_id is None  # working head 已释放
    assert draft.status == "failed"
    failed_event = await db_session.scalar(
        select(AgentEvent).where(
            AgentEvent.run_id == run_row.id,
            AgentEvent.event_type == "run.failed",
        )
    )
    assert failed_event is not None

    # 失败后可接管：下一次 create 新建 Run 并成功发布。
    gateway.actions = _make_actions(db_session, evidence, _cache_state())
    summary = await service.create(user.id, session.id, PLATFORM, KOL_UID)
    assert summary.cached is False
    assert summary.run_id is not None
    assert summary.run_id != run_row.id
    assert summary.detail is not None


async def test_engine_exception_settles_run_failed_and_releases_working_head(
    db_session, user_factory, monkeypatch
) -> None:
    """引擎抛出未捕获异常（G3）：协调行已提交不能整单回滚——服务把 Run 置
    failed、释放 working head 并提交，再把原异常抛给上层（不遮蔽）。"""
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    # 服务的异常路径会 rollback（会话内全部 ORM 对象过期）：先把 id 取到局部变量。
    user_id, session_id = user.id, session.id
    evidence = await _make_evidence(db_session, user_id, session_id)
    evidence_id = evidence.id
    _, service = _make_service(
        db_session,
        actions=_make_actions(db_session, evidence, _cache_state()),
        evidence=evidence,
        now_fn=lambda: T0,
    )

    async def _boom(**kwargs):
        raise RuntimeError("engine exploded")

    monkeypatch.setattr(service._engine, "run", _boom)

    with pytest.raises(RuntimeError, match="engine exploded"):
        await service.create(user_id, session_id, PLATFORM, KOL_UID)

    run_row = await db_session.scalar(
        select(AgentRun).where(
            AgentRun.session_id == session_id,
            AgentRun.profile_name == "kol_detail_v1",
        )
    )
    assert run_row is not None
    assert run_row.status == RunStatus.FAILED
    draft = await db_session.scalar(
        select(ArtifactDraft).where(ArtifactDraft.session_id == session_id)
    )
    assert draft is not None
    assert draft.owner_run_id is None
    assert draft.status == "failed"
    failed_event = await db_session.scalar(
        select(AgentEvent).where(
            AgentEvent.run_id == run_row.id,
            AgentEvent.event_type == "run.failed",
        )
    )
    assert failed_event is not None

    # 异常收口后新服务实例可正常接管（rollback 已过期旧 ORM 对象，重新查询）。
    fresh_evidence = await db_session.get(EvidenceItem, evidence_id)
    gateway2, service2 = _make_service(
        db_session,
        actions=_make_actions(db_session, fresh_evidence, _cache_state()),
        evidence=fresh_evidence,
        now_fn=lambda: T0,
    )
    summary = await service2.create(user_id, session_id, PLATFORM, KOL_UID)
    assert summary.cached is False
    assert summary.run_id != run_row.id
    assert summary.detail is not None


async def test_takeover_restores_kol_detail_trigger_context(db_session, user_factory) -> None:
    """崩溃接管（G3）：会话里先有一条无关用户消息，kol_detail Run 崩溃后被
    接管时发给模型的触发上下文仍指向正确的 platform/kol_uid（经
    transcript 显式锚点 + prompt_snapshot，不拿会话最近消息顶替）。"""
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    now = utc_now()
    # 会话里先有一条无关普通用户消息（绝不可被当作 kol_detail 的触发上下文）。
    db_session.add(
        AgentMessage(
            id=str(uuid4()),
            session_id=session.id,
            run_id=None,
            role="user",
            content="给我看看上个月的品牌声量",
            metadata_json=None,
            sequence=1,
            created_at=now,
        )
    )
    await db_session.flush()
    evidence = await _make_evidence(db_session, user.id, session.id)

    # 崩溃残留的 kol_detail Run：running + 过期租约 + prompt_snapshot 触发
    # 上下文 + 协调行（Artifact/Draft owner）+ 一个已完成的抓取 Step。
    run = AgentRun(
        id=str(uuid4()),
        session_id=session.id,
        user_id=user.id,
        input_message_id=None,
        run_kind="user",
        visibility="user",
        profile_name="kol_detail_v1",
        profile_version="v1",
        model="test-model",
        prompt_snapshot_json=build_kol_detail_prompt_snapshot(
            platform=PLATFORM,
            kol_uid=KOL_UID,
            selection_artifact_id=None,
            selection_version=None,
        ),
        status="running",
        decision_count=0,
        review_count=0,
        revision_count=0,
        started_at=now,
        lease_owner="dead-worker",
        lease_expires_at=now - timedelta(seconds=1),
    )
    db_session.add(run)
    await db_session.flush()
    attempt1 = AgentRunAttempt(
        id=str(uuid4()), run_id=run.id, attempt=1, started_at=now, outcome="running"
    )
    db_session.add(attempt1)
    await db_session.flush()
    artifact = AgentArtifact(
        id=str(uuid4()),
        session_id=session.id,
        user_id=user.id,
        module="kol-detail",
        artifact_type="kol_detail_v2",
        artifact_key=f"kol-detail:{PLATFORM}:{KOL_UID}",
        status="draft",
        latest_version=0,
        activity_sequence=0,
        created_at=now,
        updated_at=now,
    )
    db_session.add(artifact)
    await db_session.flush()
    db_session.add(
        ArtifactDraft(
            id=str(uuid4()),
            artifact_id=artifact.id,
            session_id=session.id,
            owner_run_id=run.id,
            current_revision=0,
            status="drafting",
            review_count=0,
            revision_count=0,
            updated_at=now,
        )
    )
    db_session.add(
        AgentStep(
            id=str(uuid4()),
            run_id=run.id,
            attempt_id=attempt1.id,
            sequence=1,
            step_type="tool_call",
            input_json={
                "internal_tool_name": "kol_detail_fetch",
                "arguments": {"platform": PLATFORM, "kol_uid": KOL_UID},
            },
            output_json={
                "status": "success",
                "safe_summary": "kol detail fetched",
                "evidence_id": evidence.id,
                "cursor": None,
                "truncated": False,
                "error_type": None,
            },
            status="completed",
            visibility="user",
            created_at=now,
        )
    )
    await db_session.flush()

    # 接管（与 executor 的过期租约路径一致）：领取 → pause 旧 Attempt →
    # begin_attempt(resumed) → 重新领取；transcript 重建上下文。
    repo = AgentRunRepository(db_session)
    transcript = await RunTranscriptLoader(db_session).load(run)
    assert transcript.user_question == kol_detail_trigger_content(PLATFORM, KOL_UID)
    assert transcript.resume_step is None  # 抓取 Step 已完成，无需复用
    assert await repo.claim_lease(run.id, "worker", 300)
    assert await repo.pause(run.id, "worker")
    attempt2 = await repo.begin_attempt(run.id, resumed=True)
    assert await repo.claim_lease(run.id, "worker", 300)

    # 恢复后不重新抓取：直接 create_draft（复用既有 working head）→ submit。
    build = build_kol_detail_draft(
        platform=PLATFORM,
        kol_uid=KOL_UID,
        selection_artifact_id=None,
        selection_version=None,
        detail=DETAIL,
        evidence_id=evidence.id,
        cache_state=_cache_state(),
    )

    async def submit(resumed_run):
        draft = await db_session.scalar(
            select(ArtifactDraft).where(ArtifactDraft.owner_run_id == resumed_run.id)
        )
        assert draft is not None
        return SubmitReview(
            action="submit_review",
            artifact_draft_ids=(draft.id,),
            completion_text="达人详情已完成",
            summary="达人详情",
        )

    actions = [
        CallTool(
            action="call_tool",
            internal_tool_name="create_draft",
            arguments={
                "module": build.module,
                "schema_version": build.schema_version,
                "artifact_type": build.artifact_type,
                "business_fields": build.business_fields,
                "payload": build.payload,
                "evidence_refs": build.evidence_refs,
            },
            rationale="创建达人详情 Draft",
        ),
        submit,
    ]
    gateway, service = _make_service(
        db_session, actions=actions, evidence=evidence, now_fn=lambda: T0
    )

    outcome = await service._engine.run(
        run=run,
        attempt_id=attempt2.id,
        profile=get_profile("kol_detail_v1"),
        messages=transcript.messages,
        thinking_sink=service._engine.thinking_sink_for(run),
        resume_step=transcript.resume_step,
        user_question=transcript.user_question,
    )

    assert outcome.status == RunStatus.COMPLETED
    # 发给模型的触发上下文：messages[0] 是 kol_detail 触发消息（含正确
    # platform/kol_uid），Memory Header 的 current_user_message 同锚点——
    # 都不是会话里那条无关用户消息。
    first_context = gateway.calls[0]["messages"]
    assert first_context[1].content == kol_detail_trigger_content(PLATFORM, KOL_UID)
    assert "品牌声量" not in first_context[1].content
    header = json.loads(first_context[0].content)
    assert header["current_user_message"] == kol_detail_trigger_content(PLATFORM, KOL_UID)
    assert "品牌声量" not in header["current_user_message"]
    # 已发布 kol_detail_v2。
    version = await db_session.scalar(
        select(AgentArtifactVersion).where(AgentArtifactVersion.source_run_id == run.id)
    )
    assert version is not None
