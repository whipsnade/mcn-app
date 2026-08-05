"""kol_detail 并发请求协调的真实并发集成测试（Gate A G3 / P0）。

与单会话 TOCTOU 用例（test_kol_detail.py 的 interleave 测试）不同，这里用
**两个独立连接**（真实 ``SessionFactory``、数据真实提交）驱动两个并发
``KolDetailRunService.create``，验证数据库级请求协调：

- 恰好创建一个 kol_detail Run（先到者持有协调行，后到者幂等返回）；
- MCP 传输最多外发一次（fake transport 只编排一条成功结果，二次外发即失败）；
- 钱包恰好结算一次 10 积分（绝不重复扣费）。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
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
    ArtifactPublishAttempt,
    ArtifactReviewBatch,
    ArtifactReviewItem,
    KolDetailCache,
)
from app.agent_runtime.engine import AgentEngine
from app.agent_runtime.events import AgentEventBroker, AgentEventStream
from app.agent_runtime.kol_detail import KolDetailRunService
from app.agent_runtime.models import (
    AgentEvent,
    AgentMessage,
    AgentRun,
    AgentSession,
    AgentToolCall,
    EvidenceItem,
    MemoryEntry,
)
from app.agent_runtime.schemas import CallTool, Complete, PublishArtifacts
from app.agent_runtime.tools.builders import BuildKolDetailDraftTool
from app.agent_runtime.tools.mcp import MCP_POINTS_COST, AgentMcpTool
from app.agent_runtime.tools.registry import McpCatalogEntry, ToolRegistry
from app.billing.models import Wallet
from app.db.session import SessionFactory
from app.identity.models import User
from app.mcp_gateway.contracts import DataTapService
from app.mcp_gateway.transport import RemoteToolResult

PLATFORM = "xiaohongshu"
KOL_UID = "k-concurrent"
T0 = datetime(2026, 1, 1, 12, 0, 0)

INPUT_SCHEMA = {
    "type": "object",
    "properties": {"platform": {"type": "string"}, "kol_uid": {"type": "string"}},
    "additionalProperties": False,
}
# 输出 Schema（封闭式，与 DETAIL 结构对齐）：raw_payload 即 DETAIL 全文，
# lineage 校验按 ``/metrics/followers`` 等 JSON Pointer 在 raw_payload 内解析
# 来源路径；网关校验策略要求每层对象拒绝 additionalProperties。
_DISTRIBUTION_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "key": {"type": "string"},
            "label": {"type": "string"},
            "value": {"type": "number"},
            "share": {"type": "number"},
        },
        "additionalProperties": False,
    },
}
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "identity": {
            "type": "object",
            "properties": {
                "nickname": {"type": "string"},
                "avatar_url": {"type": "string"},
                "homepage_url": {"type": "string"},
                "bio": {"type": "string"},
                "verification": {"type": "boolean"},
                "region": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "metrics": {
            "type": "object",
            "properties": {
                "followers": {"type": "number"},
                "following": {"type": "number"},
                "posts": {"type": "number"},
                "likes": {"type": "number"},
                "active_followers": {"type": "number"},
                "active_follower_rate": {"type": "number"},
                "growth_rate": {"type": "number"},
                "engagement_total": {"type": "number"},
                "avg_engagement": {"type": "number"},
            },
            "additionalProperties": False,
        },
        "audience": {
            "type": "object",
            "properties": {
                "gender_distribution": _DISTRIBUTION_SCHEMA,
                "age_distribution": _DISTRIBUTION_SCHEMA,
                "region_distribution": _DISTRIBUTION_SCHEMA,
                "interest_distribution": _DISTRIBUTION_SCHEMA,
            },
            "additionalProperties": False,
        },
        "trend": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string"},
                    "followers": {"type": "number"},
                    "engagement": {"type": "number"},
                    "posts": {"type": "number"},
                },
                "additionalProperties": False,
            },
        },
        "latest_posts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "post_id": {"type": "string"},
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "published_at": {"type": "string"},
                    "likes": {"type": "number"},
                    "comments": {"type": "number"},
                    "shares": {"type": "number"},
                    "engagement": {"type": "number"},
                },
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}

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

CACHE_STATE = {
    "hit": False,
    "fetched_at": T0.isoformat(),
    "expires_at": datetime(2026, 1, 2, 12, 0, 0).isoformat(),
}


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FetchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: str = ""
    kol_uid: str = ""


class OneShotMcpTransport:
    """只编排一条成功结果：第二次外发直接 AssertionError（重复抓取防线）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[DataTapService, str, dict[str, Any]]] = []
        self._outcomes: list[RemoteToolResult] = [
            RemoteToolResult(
                structured_content=DETAIL,
                is_error=False,
                upstream_request_id="req-kol-detail-1",
            )
        ]

    async def call_tool(
        self, service: DataTapService, remote_name: str, arguments: Any
    ) -> RemoteToolResult:
        self.calls.append((service, remote_name, dict(arguments)))
        if not self._outcomes:
            raise AssertionError("unexpected second MCP dispatch")
        return self._outcomes.pop(0)

    async def reconcile_tool_call(self, upstream_request_id: str) -> None:
        return None


class ScriptedGateway:
    """脚本化动作网关；create_draft 动作在运行时解析本 Run 的 Evidence id。"""

    def __init__(self, actions: list[Any]) -> None:
        self.actions = list(actions)
        self.calls: list[dict[str, Any]] = []

    async def decide(
        self, *, run, attempt_id, profile, messages, thinking_sink=None, **kwargs
    ) -> Any:
        self.calls.append({"run_id": run.id, "messages": list(messages)})
        if not self.actions:
            raise AssertionError("scripted gateway exhausted")
        action = self.actions.pop(0)
        if callable(action):
            action = await action(run)
        return action


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------


def _actions_for(db) -> list[Any]:
    """kol_detail_v1 脚本：MCP 抓取 → build_kol_detail_draft（运行时取 Evidence）→
    直接发布 → complete。

    H2 起 create_draft 对 kol_detail_v2 直写被 typed_artifact_requires_builder
    护栏拒绝，脚本与生产语义一致走 Builder 工具；直接发布改造后由
    publish_artifacts（确定性校验，无模型 Reviewer）发布。
    """

    async def create_draft(run):
        evidence = await db.scalar(
            select(EvidenceItem).where(EvidenceItem.run_id == run.id)
        )
        assert evidence is not None
        return CallTool(
            action="call_tool",
            internal_tool_name="build_kol_detail_draft",
            arguments={
                "platform": PLATFORM,
                "kol_uid": KOL_UID,
                "evidence_id": evidence.id,
                "cache_state": CACHE_STATE,
            },
            rationale="创建达人详情 Draft",
        )

    async def publish(run):
        draft = await db.scalar(
            select(ArtifactDraft).where(ArtifactDraft.owner_run_id == run.id)
        )
        assert draft is not None
        return PublishArtifacts(
            action="publish_artifacts",
            artifact_draft_ids=(draft.id,),
            summary="达人详情",
        )

    return [
        CallTool(
            action="call_tool",
            internal_tool_name="kol_detail",
            arguments={"platform": PLATFORM, "kol_uid": KOL_UID},
            rationale="抓取达人详情",
        ),
        create_draft,
        publish,
        Complete(action="complete", text="达人详情已完成"),
    ]


def _make_service(db, *, gateway: ScriptedGateway, transport, worker: str):
    entry = McpCatalogEntry(
        internal_tool_name="kol_detail",
        service_slug="insight-cube-mcp",
        reviewed_description="kol detail fetch",
        input_schema_json=INPUT_SCHEMA,
        review_status="approved",
        is_enabled=True,
    )
    registry = ToolRegistry(
        catalog_source=[entry],
        mcp_executor_factory=lambda row: AgentMcpTool(
            internal_name=row.internal_tool_name,
            service=DataTapService.INSIGHT_CUBE,
            remote_name="datatap.kol.detail.v1",
            input_schema=row.input_schema_json,
            output_schema=OUTPUT_SCHEMA,
            transport=transport,
        ),
    )
    registry.register(BuildKolDetailDraftTool(db), category="artifact")
    broker = AgentEventBroker()
    events = AgentEventStream(db, broker)
    engine = AgentEngine(
        db,
        gateway=gateway,
        registry=registry,
        events=events,
        worker_id=worker,
    )
    return KolDetailRunService(
        db, engine=engine, worker_id=worker, cache_ttl_hours=24, model="test-model"
    )


async def _teardown(user_id: str, session_id: str) -> None:
    """清理真实提交的测试数据（按 FK 依赖顺序，最后删 user 级联收尾）。"""
    async with SessionFactory() as db:
        run_ids = list(
            (
                await db.scalars(
                    select(AgentRun.id).where(AgentRun.session_id == session_id)
                )
            ).all()
        )
        child_run_ids = list(
            (
                await db.scalars(
                    select(AgentRun.id).where(AgentRun.parent_run_id.in_(run_ids))
                )
            ).all()
        ) if run_ids else []
        all_run_ids = run_ids + child_run_ids
        artifact_ids = list(
            (
                await db.scalars(
                    select(AgentArtifact.id).where(AgentArtifact.session_id == session_id)
                )
            ).all()
        )
        batch_ids = list(
            (
                await db.scalars(
                    select(ArtifactReviewBatch.id).where(
                        ArtifactReviewBatch.parent_run_id.in_(run_ids)
                    )
                )
            ).all()
        ) if run_ids else []
        await db.execute(
            delete(EvidenceItem).where(EvidenceItem.session_id == session_id)
        )
        if all_run_ids:
            await db.execute(
                delete(AgentEvent).where(AgentEvent.run_id.in_(all_run_ids))
            )
            await db.execute(
                delete(AgentToolCall).where(AgentToolCall.run_id.in_(all_run_ids))
            )
        if batch_ids:
            await db.execute(
                delete(ArtifactReviewItem).where(
                    ArtifactReviewItem.batch_id.in_(batch_ids)
                )
            )
        if run_ids:
            await db.execute(
                delete(ArtifactReviewBatch).where(
                    ArtifactReviewBatch.parent_run_id.in_(run_ids)
                )
            )
        # 直接发布留痕（FK 引用 run/artifact/revision/version）：先于版本与草稿删除。
        if all_run_ids:
            await db.execute(
                delete(ArtifactPublishAttempt).where(
                    ArtifactPublishAttempt.run_id.in_(all_run_ids)
                )
            )
        # artifact_events.artifact_version_id FK → versions：先删事件再删版本。
        await db.execute(
            delete(ArtifactEvent).where(ArtifactEvent.session_id == session_id)
        )
        if artifact_ids:
            await db.execute(
                delete(AgentArtifactVersion).where(
                    AgentArtifactVersion.artifact_id.in_(artifact_ids)
                )
            )
            await db.execute(
                delete(ArtifactDraftRevision).where(
                    ArtifactDraftRevision.artifact_id.in_(artifact_ids)
                )
            )
        await db.execute(
            delete(ArtifactDraft).where(ArtifactDraft.session_id == session_id)
        )
        await db.execute(
            delete(AgentArtifact).where(AgentArtifact.session_id == session_id)
        )
        await db.execute(
            delete(KolDetailCache).where(KolDetailCache.session_id == session_id)
        )
        # 无级联的 Run 旁挂行（自引用/消息/记忆）先于 user 级联删除。
        await db.execute(
            delete(AgentMessage).where(AgentMessage.session_id == session_id)
        )
        await db.execute(
            delete(MemoryEntry).where(MemoryEntry.session_id == session_id)
        )
        if child_run_ids:
            await db.execute(delete(AgentRun).where(AgentRun.id.in_(child_run_ids)))
        user = await db.get(User, user_id)
        if user is not None:
            await db.delete(user)
        await db.commit()


# ---------------------------------------------------------------------------
# 真实并发：恰好一次抓取 / 恰好一次扣费
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_create_fetches_and_charges_exactly_once() -> None:
    user_id = str(uuid4())
    session_id = str(uuid4())
    now = _now()
    async with SessionFactory.begin() as setup:
        setup.add(
            User(
                id=user_id,
                nickname="并发协调测试用户",
                role="user",
                status="active",
                created_at=now,
                updated_at=now,
            )
        )
        await setup.flush()
        setup.add(
            Wallet(user_id=user_id, balance=1000, reserved=0, version=0, updated_at=now)
        )
        setup.add(
            AgentSession(
                id=session_id,
                user_id=user_id,
                title="并发协调会话",
                status="active",
                created_at=now,
                updated_at=now,
            )
        )

    transport = OneShotMcpTransport()
    start = asyncio.Event()
    ready_count = 0

    async def create(worker: str):
        nonlocal ready_count
        async with SessionFactory() as db:
            gateway = ScriptedGateway(_actions_for(db))
            service = _make_service(db, gateway=gateway, transport=transport, worker=worker)
            ready_count += 1
            if ready_count == 2:
                start.set()
            await start.wait()
            summary = await service.create(user_id, session_id, PLATFORM, KOL_UID)
            await db.commit()  # 与路由层一致：引擎事务（发布 + 缓存回填 + 终态）提交
            return summary

    try:
        results = await asyncio.gather(
            create("worker-a"), create("worker-b"), return_exceptions=True
        )
        assert not any(isinstance(result, BaseException) for result in results), results
        summaries = list(results)

        async with SessionFactory() as verify:
            # 恰好一个 kol_detail Run。
            run_ids = list(
                (
                    await verify.scalars(
                        select(AgentRun.id).where(
                            AgentRun.session_id == session_id,
                            AgentRun.profile_name == "kol_detail_v1",
                        )
                    )
                ).all()
            )
            assert len(run_ids) == 1
            # MCP 传输恰好外发一次（OneShotMcpTransport 对第二次外发也会断言失败）。
            assert len(transport.calls) == 1
            # 钱包恰好结算一次 10 积分。
            wallet = await verify.get(Wallet, user_id)
            assert wallet is not None
            assert (wallet.balance, wallet.reserved) == (1000 - MCP_POINTS_COST, 0)
            settled_calls = await verify.scalar(
                select(func.count(AgentToolCall.id)).where(
                    AgentToolCall.run_id.in_(run_ids),
                    AgentToolCall.points_settled == MCP_POINTS_COST,
                )
            )
            assert settled_calls == 1

        # 两个响应一致收敛：winner 携 detail/run_id，loser 幂等指向同一 Run
        # （或 winner 已完成后 loser 直接缓存命中）——绝不各建各的 Run。
        run_ids = {summary.run_id for summary in summaries if summary.run_id is not None}
        assert len(run_ids) == 1
        assert any(
            summary.detail is not None or summary.cached for summary in summaries
        )
    finally:
        await _teardown(user_id, session_id)


# ---------------------------------------------------------------------------
# 已发布 Version 回退（H2）：发布窗口 / 恢复不回填下零重复扣费
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_published_version_fallback_serves_without_recharge() -> None:
    """已发布 Version 回退（H2）：Version 已发布 + 缓存为空（发布窗口 /
    executor 恢复不回填的真实提交状态）→ 第二个 create 由 Version 重建缓存
    并命中——零新 Run、零 MCP 外发、钱包不变。
    """
    user_id = str(uuid4())
    session_id = str(uuid4())
    now = _now()
    async with SessionFactory.begin() as setup:
        setup.add(
            User(
                id=user_id,
                nickname="Version 回退测试用户",
                role="user",
                status="active",
                created_at=now,
                updated_at=now,
            )
        )
        await setup.flush()
        setup.add(
            Wallet(user_id=user_id, balance=1000, reserved=0, version=0, updated_at=now)
        )
        setup.add(
            AgentSession(
                id=session_id,
                user_id=user_id,
                title="Version 回退会话",
                status="active",
                created_at=now,
                updated_at=now,
            )
        )

    transport = OneShotMcpTransport()
    try:
        # 第一次 create：真实 Run 抓取 + 发布 + 回填缓存（与路由层一致提交）。
        async with SessionFactory() as db:
            gateway = ScriptedGateway(_actions_for(db))
            service = _make_service(db, gateway=gateway, transport=transport, worker="worker-a")
            summary1 = await service.create(user_id, session_id, PLATFORM, KOL_UID)
            await db.commit()
        assert summary1.cached is False
        assert summary1.run_id is not None
        assert len(transport.calls) == 1

        # 模拟发布窗口 / 恢复不回填：Version 已发布、working head 已释放、缓存为空。
        async with SessionFactory.begin() as db:
            await db.execute(
                delete(KolDetailCache).where(KolDetailCache.session_id == session_id)
            )

        # 第二次 create：命中最新已发布 Version（等价缓存命中）。gateway 不编排
        # 任何动作——任何 decide 都会耗尽报错；transport 二次外发同样断言失败。
        async with SessionFactory() as db:
            gateway2 = ScriptedGateway([])
            service2 = _make_service(db, gateway=gateway2, transport=transport, worker="worker-b")
            summary2 = await service2.create(user_id, session_id, PLATFORM, KOL_UID)
            await db.commit()
        assert summary2.cached is True
        assert summary2.run_id is None
        assert summary2.detail is not None
        assert summary2.detail["data"]["cache"]["hit"] is True

        async with SessionFactory() as verify:
            # 零新 Run / 零新 MCP 外发 / 钱包不变（绝不重复扣费）。
            run_count = await verify.scalar(
                select(func.count(AgentRun.id)).where(
                    AgentRun.session_id == session_id,
                    AgentRun.profile_name == "kol_detail_v1",
                )
            )
            assert run_count == 1
            assert len(transport.calls) == 1
            wallet = await verify.get(Wallet, user_id)
            assert wallet is not None
            assert (wallet.balance, wallet.reserved) == (1000 - MCP_POINTS_COST, 0)
            # 缓存行已由 Version 重建：fetched_at/expires_at 与 Version 发布时间对齐。
            version = await verify.scalar(
                select(AgentArtifactVersion).where(
                    AgentArtifactVersion.source_run_id == summary1.run_id
                )
            )
            assert version is not None
            cache_row = await verify.scalar(
                select(KolDetailCache).where(KolDetailCache.session_id == session_id)
            )
            assert cache_row is not None
            assert cache_row.fetched_at == version.created_at
            assert cache_row.expires_at == version.created_at + timedelta(hours=24)
    finally:
        await _teardown(user_id, session_id)
