"""Artifact Builder 工具测试（v3 加固 §3.3/§6.1，B2）。

五个 Builder 工具（build_brand_report_draft / build_campaign_report_draft /
build_kol_selection_draft / build_kol_analysis_draft / build_kol_detail_draft）：

- 端到端：塞 Evidence → 调工具 → Draft 落库且 payload 过 ArtifactPayloadValidator；
- 输出只回 artifact_id/draft_id/revision_id/schema_version + 受限/缺失摘要，
  绝不把完整 payload 回灌模型上下文；
- 无效 Evidence ID / 跨 Session Evidence → 结构化错误回喂（不泄漏存在性）；
- 重复构建同一业务身份 → 同一 artifact 追加新 Revision（update 路径）；
- 工厂装配：五个工具注册进 ARTIFACT_TOOLS 分类，对 session_analyst_v1 /
  kol_detail_v1 可见。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from app.agent_artifacts.keys import build_artifact_key
from app.agent_artifacts.models import (
    AgentArtifact,
    AgentArtifactVersion,
    ArtifactDraftRevision,
)
from app.agent_artifacts.payloads.brand import BrandReportV3
from app.agent_artifacts.payloads.campaign import CampaignReportV2
from app.agent_artifacts.payloads.kol_analysis import KolAnalysisV2
from app.agent_artifacts.payloads.kol_detail import KolDetailV2
from app.agent_artifacts.payloads.kol_selection import KolSelectionV3
from app.agent_runtime.evidence import EvidenceWriter
from app.agent_runtime.models import (
    AgentRun,
    AgentRunAttempt,
    AgentSession,
    AgentStep,
    AgentToolCall,
)
from app.agent_runtime.profiles import ARTIFACT_TOOLS, PROFILES
from app.agent_runtime.tools.builders import (
    BuildBrandReportDraftTool,
    BuildCampaignReportDraftTool,
    BuildKolAnalysisDraftTool,
    BuildKolDetailDraftTool,
    BuildKolSelectionDraftTool,
)
from app.agent_runtime.tools.contracts import ToolContext
from app.agent_runtime.tools.factory import AgentToolRegistryFactory

session_analyst = PROFILES["session_analyst_v1"]
kol_detail_profile = PROFILES["kol_detail_v1"]

BUILDER_TOOL_NAMES = {
    "build_brand_report_draft",
    "build_campaign_report_draft",
    "build_kol_selection_draft",
    "build_kol_analysis_draft",
    "build_kol_detail_draft",
}

BRAND_SCOPE = {
    "brand": "瑞幸咖啡",
    "period": {"start": "2026-07-01", "end": "2026-07-31", "timezone": "Asia/Shanghai"},
    "platforms": ["xiaohongshu"],
    "keywords": ["瑞幸"],
    "comparison_mode": "none",
}

CAMPAIGN_SCOPE = {
    "brand": "瑞幸咖啡",
    "campaign": "生椰拿铁上新",
    "period": {"start": "2026-07-01", "end": "2026-07-31", "timezone": "Asia/Shanghai"},
    "platforms": ["xiaohongshu"],
    "keywords": ["生椰拿铁"],
}

KOL_SCOPE = {
    "category": "美食",
    "platforms": ["小红书"],
    "audience": {"regions": ["上海"], "age_ranges": ["18-24"], "interests": ["美食"]},
    "filters": {},
}


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _make_session(db_session, user_id: str) -> AgentSession:
    now = _now()
    session = AgentSession(
        id=str(uuid4()),
        user_id=user_id,
        title="会话",
        status="active",
        created_at=now,
        updated_at=now,
    )
    db_session.add(session)
    await db_session.flush()
    return session


async def _make_run(db_session, session_id: str, user_id: str) -> tuple[AgentRun, AgentStep]:
    now = _now()
    run = AgentRun(
        id=str(uuid4()),
        session_id=session_id,
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
    return run, step


async def _write_evidence(
    db_session, *, session_id: str, run_id: str, step_id: str, payload: Any
) -> str:
    now = _now()
    call = AgentToolCall(
        id=str(uuid4()),
        run_id=run_id,
        step_id=step_id,
        logical_call_id=f"call-{uuid4()}",
        service="mcp",
        internal_tool_name="query_analysis_data",
        arguments_json={},
        arguments_hash="h",
        status="settled",
        points_reserved=10,
        points_settled=10,
        started_at=now,
        completed_at=now,
    )
    db_session.add(call)
    await db_session.flush()
    item = await EvidenceWriter(db_session).write(
        session_id=session_id,
        run_id=run_id,
        tool_call_id=call.id,
        source_type="mcp",
        source_name="query_analysis_data",
        scope_json=None,
        period_json=None,
        raw_payload=payload,
    )
    return item.id


def _ctx(user_id: str, session_id: str, run_id: str, step_id: str) -> ToolContext:
    return ToolContext(
        user_id=user_id,
        session_id=session_id,
        run_id=run_id,
        profile_name="session_analyst_v1",
        step_id=step_id,
    )


def _brand_evidence_rows() -> dict[str, list[dict[str, Any]]]:
    return {
        "overview_current": [
            {
                "平台": "小红书",
                "声量": 100,
                "互动数": 1000,
                "发帖数": 80,
                "正面声量": 60,
                "中性声量": 30,
                "负面声量": 10,
            }
        ],
        "sentiment": [
            {"平台": "小红书", "情感": "正面", "声量": 60},
            {"平台": "小红书", "情感": "中性", "声量": 30},
            {"平台": "小红书", "情感": "负面", "声量": 10},
        ],
        "daily_trend": [
            {
                "日期": "2026-07-01",
                "平台": "小红书",
                "声量": 10,
                "互动数": 100,
                "正面": 6,
                "中性": 3,
                "负面": 1,
            }
        ],
        "topics": [
            {"话题": "生椰拿铁", "声量": 50, "互动数": 500, "正面": 40, "中性": 5, "负面": 5}
        ],
        "top_posts": [
            {
                "平台": "小红书",
                "帖子ID": "p1",
                "标题": "测评",
                "作者": "达人A",
                "发布时间": "2026-07-05 10:00:00",
                "点赞数": 100,
                "评论数": 20,
                "分享数": 5,
                "互动数": 125,
                "帖子链接": "https://example.com/p1",
            }
        ],
    }


def _campaign_post_rows() -> list[dict[str, Any]]:
    return [
        {
            "平台": "小红书",
            "帖子ID": "p1",
            "标题": "测评",
            "作者": "达人A",
            "用户ID": "u1",
            "发布时间": "2026-07-03 09:00:00",
            "点赞数": 100,
            "评论数": 10,
            "分享数": 5,
            "互动数": 115,
            "内容类型": "图文",
            "情感": "正面",
            "帖子链接": "https://example.com/p1",
        }
    ]


def _kol_items() -> list[dict[str, Any]]:
    return [
        {
            "platform": "小红书",
            "kol_uid": "1",
            "nickname": "达人1",
            "followers": 500_000,
            "engagement_total": 100,
            "audience": {"regions": ["上海"], "age_ranges": ["18-24"], "interests": ["美食"]},
            "score_inputs": {
                "audience_interests": {"美食": 80},
                "audience_regions": {"上海": 50},
                "audience_age": {"18-24岁": 40},
                "average_interactions": 20_000,
                "effective_follower_rate": 60,
                "active_follower_count": 300_000,
                "content_score": 90,
                "followers": 500_000,
                "interaction_follower_ratio": 3.0,
            },
        }
    ]


def _kol_detail_payload() -> dict[str, Any]:
    return {
        "identity": {
            "nickname": "达人1",
            "avatar_url": "https://example.com/a.png",
            "homepage_url": "https://example.com/h",
            "bio": "美食博主",
            "verification": True,
            "region": "上海",
        },
        "metrics": {
            "followers": 500_000,
            "following": 100,
            "posts": 200,
            "likes": 50_000,
            "active_followers": 300_000,
            "active_follower_rate": 0.6,
            "growth_rate": 0.3,
            "engagement_total": 100,
            "avg_engagement": 1.0,
        },
        "audience": {
            "gender_distribution": [{"key": "女", "label": "女", "value": 60, "share": 0.6}],
            "age_distribution": [
                {"key": "18-24", "label": "18-24", "value": 40, "share": 0.4}
            ],
            "region_distribution": [{"key": "上海", "label": "上海", "value": 50, "share": 0.5}],
            "interest_distribution": [
                {"key": "美食", "label": "美食", "value": 80, "share": 0.8}
            ],
        },
        "trend": [{"date": "2026-07-01", "followers": 500_000, "engagement": 100, "posts": 2}],
        "latest_posts": [
            {
                "post_id": "p1",
                "title": "测评",
                "url": "https://example.com/p1",
                "published_at": "2026-07-02T10:00:00",
                "likes": 10,
                "comments": 2,
                "shares": 1,
                "engagement": 13,
            }
        ],
    }


async def _latest_revision_payload(db_session, artifact_id: str) -> dict[str, Any]:
    revision = await db_session.scalar(
        select(ArtifactDraftRevision)
        .where(ArtifactDraftRevision.artifact_id == artifact_id)
        .order_by(ArtifactDraftRevision.revision.desc())
    )
    assert revision is not None
    return revision.payload_json


def _assert_summary_shape(summary: dict[str, Any]) -> None:
    """工具输出契约：只回身份 + 受限摘要，绝不回灌完整 payload。"""
    assert set(summary) == {
        "artifact_id",
        "artifact_key",
        "draft_id",
        "revision_id",
        "revision",
        "schema_version",
        "data_status",
        "restricted_sections",
        "limitations",
    }


# ---------------------------------------------------------------------------
# brand / campaign Builder 工具端到端
# ---------------------------------------------------------------------------


async def test_build_brand_report_draft_tool_end_to_end(
    db_session, user_factory
) -> None:
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    run, step = await _make_run(db_session, session.id, user.id)
    groups = _brand_evidence_rows()
    evidence_ids = {
        group: [
            await _write_evidence(
                db_session,
                session_id=session.id,
                run_id=run.id,
                step_id=step.id,
                payload=rows,
            )
        ]
        for group, rows in groups.items()
    }

    tool = BuildBrandReportDraftTool(db_session)
    result = await tool.execute(
        _ctx(user.id, session.id, run.id, step.id),
        {
            "scope": BRAND_SCOPE,
            "evidence": {group: ids for group, ids in evidence_ids.items()},
            "narrative": {"executive_summary": "品牌概览。", "findings": [], "recommendations": []},
        },
    )
    assert result.status == "success", result.safe_summary
    summary = json.loads(result.safe_summary)
    _assert_summary_shape(summary)
    assert summary["schema_version"] == "brand_report_v3"
    assert summary["data_status"] == "complete"
    assert summary["artifact_key"] == build_artifact_key("brand", brand="瑞幸咖啡")

    payload = await _latest_revision_payload(db_session, summary["artifact_id"])
    BrandReportV3.model_validate(payload)
    assert payload["data"]["overview"]["total_volume"] == 100


async def test_build_brand_report_draft_tool_second_call_advances_revision(
    db_session, user_factory
) -> None:
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    run, step = await _make_run(db_session, session.id, user.id)
    groups = _brand_evidence_rows()
    evidence_args = {}
    for group, rows in groups.items():
        evidence_args[group] = [
            await _write_evidence(
                db_session,
                session_id=session.id,
                run_id=run.id,
                step_id=step.id,
                payload=rows,
            )
        ]

    tool = BuildBrandReportDraftTool(db_session)
    ctx = _ctx(user.id, session.id, run.id, step.id)
    args = {"scope": BRAND_SCOPE, "evidence": evidence_args}
    first = json.loads((await tool.execute(ctx, args)).safe_summary)
    second = json.loads((await tool.execute(ctx, args)).safe_summary)
    assert first["artifact_id"] == second["artifact_id"]
    assert first["draft_id"] == second["draft_id"]
    assert second["revision"] == first["revision"] + 1


async def test_build_campaign_report_draft_tool_end_to_end(
    db_session, user_factory
) -> None:
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    run, step = await _make_run(db_session, session.id, user.id)
    posts_id = await _write_evidence(
        db_session,
        session_id=session.id,
        run_id=run.id,
        step_id=step.id,
        payload=_campaign_post_rows(),
    )

    tool = BuildCampaignReportDraftTool(db_session)
    result = await tool.execute(
        _ctx(user.id, session.id, run.id, step.id),
        {"scope": CAMPAIGN_SCOPE, "evidence": {"posts": [posts_id]}},
    )
    assert result.status == "success", result.safe_summary
    summary = json.loads(result.safe_summary)
    _assert_summary_shape(summary)
    assert summary["schema_version"] == "campaign_report_v2"
    assert summary["artifact_key"] == build_artifact_key(
        "campaign", brand="瑞幸咖啡", campaign="生椰拿铁上新"
    )

    payload = await _latest_revision_payload(db_session, summary["artifact_id"])
    CampaignReportV2.model_validate(payload)
    assert payload["data"]["overview"]["total_engagement"] == 115


async def test_builder_tool_restricted_summary_discloses_missing_sections(
    db_session, user_factory
) -> None:
    """某必需章节 Evidence 缺失 → 工具仍成功，输出携带受限章节与 limitation 摘要。"""
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    run, step = await _make_run(db_session, session.id, user.id)
    groups = _brand_evidence_rows()
    del groups["topics"]
    evidence_args = {}
    for group, rows in groups.items():
        evidence_args[group] = [
            await _write_evidence(
                db_session,
                session_id=session.id,
                run_id=run.id,
                step_id=step.id,
                payload=rows,
            )
        ]

    tool = BuildBrandReportDraftTool(db_session)
    result = await tool.execute(
        _ctx(user.id, session.id, run.id, step.id),
        {"scope": BRAND_SCOPE, "evidence": evidence_args},
    )
    assert result.status == "success", result.safe_summary
    summary = json.loads(result.safe_summary)
    assert summary["data_status"] == "restricted"
    assert summary["restricted_sections"]["topics"]["status"] == "unavailable"
    assert any(item["code"] == "no_evidence" for item in summary["limitations"])


# ---------------------------------------------------------------------------
# 结构化错误：无效 / 跨 Session Evidence
# ---------------------------------------------------------------------------


async def test_builder_tool_rejects_unknown_evidence_id(db_session, user_factory) -> None:
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    run, step = await _make_run(db_session, session.id, user.id)

    tool = BuildBrandReportDraftTool(db_session)
    result = await tool.execute(
        _ctx(user.id, session.id, run.id, step.id),
        {"scope": BRAND_SCOPE, "evidence": {"overview_current": ["ev-bogus"]}},
    )
    assert result.status == "failed"
    assert result.error_type == "evidence_not_found"


async def test_builder_tool_rejects_cross_session_evidence(db_session, user_factory) -> None:
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    other_session = await _make_session(db_session, user.id)
    run, step = await _make_run(db_session, session.id, user.id)
    other_run, other_step = await _make_run(db_session, other_session.id, user.id)
    foreign_evidence = await _write_evidence(
        db_session,
        session_id=other_session.id,
        run_id=other_run.id,
        step_id=other_step.id,
        payload=_campaign_post_rows(),
    )

    tool = BuildCampaignReportDraftTool(db_session)
    result = await tool.execute(
        _ctx(user.id, session.id, run.id, step.id),
        {"scope": CAMPAIGN_SCOPE, "evidence": {"posts": [foreign_evidence]}},
    )
    assert result.status == "failed"
    # 跨 Session 按 not_found 处理，不泄漏证据存在性。
    assert result.error_type == "evidence_not_found"


# ---------------------------------------------------------------------------
# kol Builder 工具端到端
# ---------------------------------------------------------------------------


async def test_build_kol_selection_draft_tool_end_to_end(db_session, user_factory) -> None:
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    run, step = await _make_run(db_session, session.id, user.id)
    evidence_id = await _write_evidence(
        db_session,
        session_id=session.id,
        run_id=run.id,
        step_id=step.id,
        payload=_kol_items(),
    )

    tool = BuildKolSelectionDraftTool(db_session)
    result = await tool.execute(
        _ctx(user.id, session.id, run.id, step.id),
        {"scope": KOL_SCOPE, "evidence_id": evidence_id},
    )
    assert result.status == "success", result.safe_summary
    summary = json.loads(result.safe_summary)
    _assert_summary_shape(summary)
    assert summary["schema_version"] == "kol_selection_v3"

    payload = await _latest_revision_payload(db_session, summary["artifact_id"])
    KolSelectionV3.model_validate(payload)
    assert payload["data"]["items"][0]["kol_uid"] == "1"

    # rank_kols 确定性评分已落库为 settled 内部零积分调用（lineage derivation 基座）。
    rank_call = await db_session.scalar(
        select(AgentToolCall).where(AgentToolCall.internal_tool_name == "rank_kols")
    )
    assert rank_call is not None
    assert rank_call.status == "settled"
    assert rank_call.service == "internal"


async def test_build_kol_analysis_draft_tool_end_to_end(db_session, user_factory) -> None:
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    run, step = await _make_run(db_session, session.id, user.id)
    ctx = _ctx(user.id, session.id, run.id, step.id)
    evidence_id = await _write_evidence(
        db_session,
        session_id=session.id,
        run_id=run.id,
        step_id=step.id,
        payload=_kol_items(),
    )

    # 先用名单 Builder 工具产出名单 Draft，再手工登记一个已发布 Version
    # （发布事务本身由 Reviewer 流程覆盖，此处直接落行模拟发布结果）。
    selection_tool = BuildKolSelectionDraftTool(db_session)
    selection_result = await selection_tool.execute(
        ctx, {"scope": KOL_SCOPE, "evidence_id": evidence_id}
    )
    assert selection_result.status == "success", selection_result.safe_summary
    selection_summary = json.loads(selection_result.safe_summary)

    revision = await db_session.scalar(
        select(ArtifactDraftRevision).where(
            ArtifactDraftRevision.id == selection_summary["revision_id"]
        )
    )
    artifact = await db_session.get(AgentArtifact, selection_summary["artifact_id"])
    assert revision is not None and artifact is not None
    version = AgentArtifactVersion(
        id=str(uuid4()),
        artifact_id=artifact.id,
        version=1,
        source_run_id=run.id,
        source_draft_revision_id=revision.id,
        schema_version=revision.schema_version,
        payload_json=revision.payload_json,
        evidence_refs_json=revision.evidence_refs_json,
        data_status=revision.payload_json["data_status"],
        created_at=_now(),
    )
    db_session.add(version)
    artifact.latest_version = 1
    artifact.status = "published"
    await db_session.flush()

    analysis_tool = BuildKolAnalysisDraftTool(db_session)
    result = await analysis_tool.execute(ctx, {"selection_artifact_id": artifact.id})
    assert result.status == "success", result.safe_summary
    summary = json.loads(result.safe_summary)
    _assert_summary_shape(summary)
    assert summary["schema_version"] == "kol_analysis_v2"

    payload = await _latest_revision_payload(db_session, summary["artifact_id"])
    KolAnalysisV2.model_validate(payload)
    assert payload["data"]["summary"]["kol_count"] == 1
    assert payload["scope"]["selection_artifact_id"] == artifact.id

    # 子 Artifact 固定到父名单 Version（不可变绑定）。
    analysis_artifact = await db_session.get(AgentArtifact, summary["artifact_id"])
    assert analysis_artifact is not None
    assert analysis_artifact.parent_artifact_id == artifact.id


async def test_build_kol_analysis_draft_tool_rejects_unpublished_selection(
    db_session, user_factory
) -> None:
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    run, step = await _make_run(db_session, session.id, user.id)

    tool = BuildKolAnalysisDraftTool(db_session)
    result = await tool.execute(
        _ctx(user.id, session.id, run.id, step.id),
        {"selection_artifact_id": "artifact-bogus"},
    )
    assert result.status == "failed"
    assert result.error_type == "not_found"


async def test_build_kol_detail_draft_tool_end_to_end(db_session, user_factory) -> None:
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    run, step = await _make_run(db_session, session.id, user.id)
    evidence_id = await _write_evidence(
        db_session,
        session_id=session.id,
        run_id=run.id,
        step_id=step.id,
        payload=_kol_detail_payload(),
    )

    tool = BuildKolDetailDraftTool(db_session)
    result = await tool.execute(
        _ctx(user.id, session.id, run.id, step.id),
        {
            "platform": "xiaohongshu",
            "kol_uid": "1",
            "evidence_id": evidence_id,
            "cache_state": {
                "hit": False,
                "fetched_at": "2026-08-01T00:00:00",
                "expires_at": "2026-08-02T00:00:00",
            },
        },
    )
    assert result.status == "success", result.safe_summary
    summary = json.loads(result.safe_summary)
    _assert_summary_shape(summary)
    assert summary["schema_version"] == "kol_detail_v2"

    payload = await _latest_revision_payload(db_session, summary["artifact_id"])
    KolDetailV2.model_validate(payload)
    assert payload["data"]["identity"]["nickname"] == "达人1"


async def test_builder_tool_requires_db_session() -> None:
    tool = BuildBrandReportDraftTool(None)
    result = await tool.execute(
        ToolContext(
            user_id="u", session_id="s", run_id="r", profile_name="session_analyst_v1"
        ),
        {"scope": BRAND_SCOPE, "evidence": {"overview_current": ["ev-1"]}},
    )
    assert result.status == "failed"


# ---------------------------------------------------------------------------
# 结构化错误回喂（F1）：可预期的校验失败一律 draft_build_error，绝不冒泡为
# engine 级「failed unexpectedly」——模型要能拿字段明细自愈。
# ---------------------------------------------------------------------------


async def test_builder_tool_hallucinated_top_level_argument_structured_error(
    db_session, user_factory
) -> None:
    """模型编造顶层参数（extra=forbid）→ 结构化错误含违规字段名。"""
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    run, step = await _make_run(db_session, session.id, user.id)

    tool = BuildBrandReportDraftTool(db_session)
    result = await tool.execute(
        _ctx(user.id, session.id, run.id, step.id),
        {
            "scope": BRAND_SCOPE,
            "evidence": {"overview_current": ["ev-1"]},
            "narratives": {"executive_summary": "编造的字段"},
        },
    )
    assert result.status == "failed"
    assert result.error_type == "draft_build_error"
    assert "narratives" in result.safe_summary


async def test_build_kol_selection_draft_tool_hallucinated_filter_field_structured_error(
    db_session, user_factory
) -> None:
    """真实 UAT 故障：filters.follower_threshold 不存在 → Pydantic 校验失败
    必须转为 draft_build_error（含字段明细），不能冒泡为 engine 级失败。"""
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    run, step = await _make_run(db_session, session.id, user.id)
    evidence_id = await _write_evidence(
        db_session,
        session_id=session.id,
        run_id=run.id,
        step_id=step.id,
        payload=_kol_items(),
    )
    bad_scope = {
        **KOL_SCOPE,
        "filters": {"follower_threshold": 10000},
    }

    tool = BuildKolSelectionDraftTool(db_session)
    result = await tool.execute(
        _ctx(user.id, session.id, run.id, step.id),
        {"scope": bad_scope, "evidence_id": evidence_id},
    )
    assert result.status == "failed"
    assert result.error_type == "draft_build_error"
    assert "follower_threshold" in result.safe_summary
    assert "filters" in result.safe_summary


async def test_build_brand_report_draft_tool_narrative_shape_error_has_field_detail(
    db_session, user_factory
) -> None:
    """narrative 形态错误（findings 用 description 而非 detail）→ 结构化错误
    带字段级明细，模型可据此修正后重试。"""
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    run, step = await _make_run(db_session, session.id, user.id)
    groups = _brand_evidence_rows()
    evidence_args = {}
    for group, rows in groups.items():
        evidence_args[group] = [
            await _write_evidence(
                db_session,
                session_id=session.id,
                run_id=run.id,
                step_id=step.id,
                payload=rows,
            )
        ]

    tool = BuildBrandReportDraftTool(db_session)
    result = await tool.execute(
        _ctx(user.id, session.id, run.id, step.id),
        {
            "scope": BRAND_SCOPE,
            "evidence": evidence_args,
            "narrative": {
                "executive_summary": "概览。",
                "findings": [{"title": "发现", "description": "写错了字段名"}],
                "recommendations": [],
            },
        },
    )
    assert result.status == "failed"
    assert result.error_type == "draft_build_error"
    assert "detail" in result.safe_summary


async def test_builder_tool_structured_error_truncated(db_session, user_factory) -> None:
    """大量校验错误时 safe_summary 截断到合理长度，不撑爆模型上下文。"""
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    run, step = await _make_run(db_session, session.id, user.id)
    bad_args = {f"bogus_field_{index}": index for index in range(200)}
    bad_args["scope"] = BRAND_SCOPE

    tool = BuildBrandReportDraftTool(db_session)
    result = await tool.execute(
        _ctx(user.id, session.id, run.id, step.id),
        bad_args,
    )
    assert result.status == "failed"
    assert result.error_type == "draft_build_error"
    assert len(result.safe_summary) <= 2100


# ---------------------------------------------------------------------------
# 工厂装配
# ---------------------------------------------------------------------------


async def test_factory_registers_builder_tools(db_session) -> None:
    registry = AgentToolRegistryFactory().build(db_session)
    entries = {entry.internal_name: entry for entry in registry.registered_tools}
    assert BUILDER_TOOL_NAMES <= set(entries)
    for name in BUILDER_TOOL_NAMES:
        assert entries[name].category == ARTIFACT_TOOLS
        assert entries[name].points_cost == 0
        assert entries[name].external_side_effect is True
        assert entries[name].tool is not None
        # 模型需要看到输入 Schema 才能构造合法参数。
        assert entries[name].input_schema


async def test_builder_tools_visible_to_analyst_and_kol_detail_profiles(db_session) -> None:
    registry = AgentToolRegistryFactory().build(db_session)
    analyst_visible = {entry.internal_name for entry in await registry.visible_tools(session_analyst)}
    assert BUILDER_TOOL_NAMES <= analyst_visible
    kol_detail_visible = {
        entry.internal_name for entry in await registry.visible_tools(kol_detail_profile)
    }
    assert BUILDER_TOOL_NAMES <= kol_detail_visible


# ---------------------------------------------------------------------------
# kol Detail Builder 的 parent 权威绑定（B3：与 CreateDraftTool 等价，§6.4）
# ---------------------------------------------------------------------------


async def test_build_kol_detail_draft_tool_parent_overridden_by_run_snapshot(
    db_session, user_factory
) -> None:
    """Run 快照携带名单引用时，模型传错的 selection 参数被服务端权威纠正。"""
    from app.agent_runtime.kol_detail import build_kol_detail_prompt_snapshot

    user = await user_factory()
    session = await _make_session(db_session, user.id)
    run, step = await _make_run(db_session, session.id, user.id)
    # 名单引用的 parent 有外键约束：经名单 Builder 落真实的 kol-selection
    # Artifact + 已发布 Version 行（与 test_build_kol_analysis_draft_tool 同法）。
    kol_evidence_id = await _write_evidence(
        db_session,
        session_id=session.id,
        run_id=run.id,
        step_id=step.id,
        payload=_kol_items(),
    )
    selection_result = await BuildKolSelectionDraftTool(db_session).execute(
        _ctx(user.id, session.id, run.id, step.id),
        {"scope": KOL_SCOPE, "evidence_id": kol_evidence_id},
    )
    assert selection_result.status == "success", selection_result.safe_summary
    selection_summary = json.loads(selection_result.safe_summary)
    sel_revision = await db_session.get(
        ArtifactDraftRevision, selection_summary["revision_id"]
    )
    sel_artifact = await db_session.get(AgentArtifact, selection_summary["artifact_id"])
    assert sel_revision is not None and sel_artifact is not None
    sel_version = AgentArtifactVersion(
        id=str(uuid4()),
        artifact_id=sel_artifact.id,
        version=1,
        source_run_id=run.id,
        source_draft_revision_id=sel_revision.id,
        schema_version=sel_revision.schema_version,
        payload_json=sel_revision.payload_json,
        evidence_refs_json=sel_revision.evidence_refs_json,
        data_status=sel_revision.payload_json["data_status"],
        created_at=_now(),
    )
    db_session.add(sel_version)
    sel_artifact.latest_version = 1
    sel_artifact.status = "published"
    run.prompt_snapshot_json = build_kol_detail_prompt_snapshot(
        platform="xiaohongshu",
        kol_uid="1",
        selection_artifact_id=sel_artifact.id,
        selection_version="1",
        selection_version_id=sel_version.id,
    )
    await db_session.flush()
    evidence_id = await _write_evidence(
        db_session,
        session_id=session.id,
        run_id=run.id,
        step_id=step.id,
        payload=_kol_detail_payload(),
    )

    tool = BuildKolDetailDraftTool(db_session)
    result = await tool.execute(
        _ctx(user.id, session.id, run.id, step.id),
        {
            "platform": "xiaohongshu",
            "kol_uid": "1",
            "evidence_id": evidence_id,
            "cache_state": {
                "hit": False,
                "fetched_at": "2026-08-01T00:00:00",
                "expires_at": "2026-08-02T00:00:00",
            },
            # 模型传错的名单引用：parent 绑定必须以 Run 快照为准。
            "selection_artifact_id": "evil-artifact",
            "selection_version": "99",
        },
    )
    assert result.status == "success", result.safe_summary
    summary = json.loads(result.safe_summary)

    # 稳定行 parent 与 Revision 版本绑定都来自快照，而非模型传参。
    artifact = await db_session.get(AgentArtifact, summary["artifact_id"])
    assert artifact is not None
    assert artifact.parent_artifact_id == sel_artifact.id
    revision = await db_session.get(ArtifactDraftRevision, summary["revision_id"])
    assert revision is not None
    assert revision.parent_artifact_version_id == sel_version.id


async def test_build_kol_detail_draft_tool_without_snapshot_has_no_parent(
    db_session, user_factory
) -> None:
    """无名单引用的 Run：模型自报的 selection 参数不产生 parent 绑定。"""
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    run, step = await _make_run(db_session, session.id, user.id)
    evidence_id = await _write_evidence(
        db_session,
        session_id=session.id,
        run_id=run.id,
        step_id=step.id,
        payload=_kol_detail_payload(),
    )

    tool = BuildKolDetailDraftTool(db_session)
    result = await tool.execute(
        _ctx(user.id, session.id, run.id, step.id),
        {
            "platform": "xiaohongshu",
            "kol_uid": "1",
            "evidence_id": evidence_id,
            "cache_state": {
                "hit": False,
                "fetched_at": "2026-08-01T00:00:00",
                "expires_at": "2026-08-02T00:00:00",
            },
            "selection_artifact_id": "self-reported-artifact",
            "selection_version": "1",
        },
    )
    assert result.status == "success", result.safe_summary
    summary = json.loads(result.safe_summary)
    artifact = await db_session.get(AgentArtifact, summary["artifact_id"])
    assert artifact is not None
    # parent 绑定只能来自经归属校验的 Run 快照；模型自报参数不参与绑定。
    assert artifact.parent_artifact_id is None
    revision = await db_session.get(ArtifactDraftRevision, summary["revision_id"])
    assert revision is not None
    assert revision.parent_artifact_version_id is None
