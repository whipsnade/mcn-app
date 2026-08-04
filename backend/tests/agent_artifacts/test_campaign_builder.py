"""campaign_report_v2 Draft builder tests（设计 §12.1 / v3 加固 §3.3/§6.1，B2）。

覆盖：
1. 确定性聚合口径：overview 汇总、platform_contributions（share）、timeline、
   kol_contributions（contribution_share，Top20）、content_types、sentiment、
   top_posts（互动量降序）；
2. restricted 路径：posts Evidence 缺失 → 必需章节 unavailable + limitation；
3. lineage：字段级 Evidence 引用覆盖全部必选 numeric，DB freeze 校验通过；
4. 输入契约：无可用 Evidence / 非法叙事 supporting_paths → DraftBuildError。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from app.agent_artifacts.builders.campaign import (
    SCHEMA_VERSION,
    build_campaign_report_draft,
)
from app.agent_artifacts.builders.common import DraftBuildError
from app.agent_artifacts.lineage import (
    DbLineageLoader,
    LineageOwner,
    required_numeric_pointers,
    validate_and_freeze_lineage,
)
from app.agent_artifacts.payloads.campaign import CampaignReportV2

SCOPE = {
    "brand": "瑞幸咖啡",
    "campaign": "生椰拿铁上新",
    "period": {"start": "2026-07-01", "end": "2026-07-31", "timezone": "Asia/Shanghai"},
    "platforms": ["xiaohongshu", "douyin"],
    "keywords": ["生椰拿铁"],
}

NARRATIVE = {
    "executive_summary": "活动整体互动集中在抖音头部达人。",
    "phase_review": [
        {
            "phase": "爆发期",
            "detail": "7 月 4 日互动达到峰值。",
            "supporting_paths": ["data.timeline.1.engagement"],
        }
    ],
    "findings": [
        {
            "title": "达人A 跨平台贡献最高",
            "detail": "达人A 在两个平台合计互动 365。",
            "supporting_paths": ["data.kol_contributions.0.engagement"],
        }
    ],
    "recommendations": [
        {
            "title": "续投头部达人",
            "action": "与达人A 续约",
            "rationale": "贡献占比最高",
            "supporting_paths": ["data.kol_contributions.0.contribution_share"],
        }
    ],
}


def _post_rows() -> list[dict[str, Any]]:
    return [
        {
            "平台": "小红书",
            "帖子ID": "p1",
            "标题": "生椰拿铁测评",
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
        },
        {
            "平台": "小红书",
            "帖子ID": "p2",
            "标题": "门店打卡",
            "作者": "达人B",
            "用户ID": "u2",
            "发布时间": "2026-07-03 18:00:00",
            "点赞数": 50,
            "评论数": 5,
            "分享数": 0,
            "互动数": 55,
            "内容类型": "视频",
            "情感": "中性",
            "帖子链接": "https://example.com/p2",
        },
        {
            "平台": "抖音",
            "帖子ID": "p3",
            "标题": "新品开箱",
            "作者": "达人A",
            "用户ID": "u1",
            "发布时间": "2026-07-04 12:00:00",
            "点赞数": 200,
            "评论数": 30,
            "分享数": 20,
            "互动数": 250,
            "内容类型": "视频",
            "情感": "负面",
            "帖子链接": "https://example.com/p3",
        },
    ]


def _full_evidence() -> dict[str, list[tuple[str, Any]]]:
    return {"posts": [("ev-posts", _post_rows())]}


# ---------------------------------------------------------------------------
# 1. 确定性聚合口径
# ---------------------------------------------------------------------------


def test_complete_payload_deterministic_aggregation() -> None:
    build = build_campaign_report_draft(
        scope=SCOPE, evidence=_full_evidence(), narrative=NARRATIVE
    )
    payload = build.payload
    CampaignReportV2.model_validate(payload)

    assert build.module == "campaign"
    assert build.schema_version == SCHEMA_VERSION == "campaign_report_v2"
    assert build.business_fields == {"brand": "瑞幸咖啡", "campaign": "生椰拿铁上新"}
    assert payload["data_status"] == "complete"

    overview = payload["data"]["overview"]
    assert overview["total_volume"] == 3
    assert overview["total_posts"] == 3
    assert overview["total_engagement"] == 420
    assert overview["total_creators"] == 2
    # 净情感指数：(1 - 1) / 3 * 100。
    assert overview["sentiment_score"] == 0.0

    contributions = payload["data"]["platform_contributions"]
    assert [row["platform"] for row in contributions] == ["xiaohongshu", "douyin"]
    xhs = contributions[0]
    assert xhs["volume"] == 2
    assert xhs["posts"] == 2
    assert xhs["engagement"] == 170
    assert xhs["creators"] == 2
    assert xhs["share"] == pytest.approx(round(2 / 3, 4))
    douyin = contributions[1]
    assert douyin["engagement"] == 250
    assert douyin["creators"] == 1
    assert douyin["share"] == pytest.approx(round(1 / 3, 4))

    timeline = payload["data"]["timeline"]
    assert [(item["date"], item["platform"]) for item in timeline] == [
        ("2026-07-03", "xiaohongshu"),
        ("2026-07-04", "douyin"),
    ]
    assert timeline[0]["volume"] == 2
    assert timeline[0]["posts"] == 2
    assert timeline[0]["engagement"] == 170
    assert timeline[1]["engagement"] == 250

    kols = payload["data"]["kol_contributions"]
    assert [(row["platform"], row["kol_uid"]) for row in kols] == [
        ("douyin", "u1"),
        ("xiaohongshu", "u1"),
        ("xiaohongshu", "u2"),
    ]
    assert kols[0]["nickname"] == "达人A"
    assert kols[0]["posts"] == 1
    assert kols[0]["volume"] == 1
    assert kols[0]["engagement"] == 250
    assert kols[0]["contribution_share"] == pytest.approx(round(250 / 420, 4))
    assert kols[2]["contribution_share"] == pytest.approx(round(55 / 420, 4))

    content_types = {
        (row["platform"], row["type"]): row for row in payload["data"]["content_types"]
    }
    assert content_types[("xiaohongshu", "图文")]["posts"] == 1
    assert content_types[("xiaohongshu", "图文")]["engagement"] == 115
    assert content_types[("xiaohongshu", "视频")]["posts"] == 1
    assert content_types[("douyin", "视频")]["engagement"] == 250

    sentiment = payload["data"]["sentiment"]
    assert sentiment["summary"]["positive"] == {"count": 1, "share": pytest.approx(round(1 / 3, 4))}
    assert sentiment["summary"]["neutral"]["count"] == 1
    assert sentiment["summary"]["negative"]["count"] == 1
    by_platform = {row["platform"]: row for row in sentiment["by_platform"]}
    assert by_platform["douyin"]["negative"]["count"] == 1

    top_posts = payload["data"]["top_posts"]
    assert [post["post_id"] for post in top_posts] == ["p3", "p1", "p2"]
    assert top_posts[0]["engagement"] == 250
    assert top_posts[0]["likes"] == 200

    assert payload["narrative"]["phase_review"][0]["phase"] == "爆发期"


def test_kol_contributions_capped_at_20() -> None:
    rows = []
    for index in range(25):
        rows.append(
            {
                "平台": "小红书",
                "帖子ID": f"p{index}",
                "标题": f"t{index}",
                "作者": f"达人{index}",
                "用户ID": f"u{index}",
                "发布时间": "2026-07-03 09:00:00",
                "互动数": 1000 - index,
                "情感": "正面",
                "帖子链接": f"https://example.com/p{index}",
            }
        )
    build = build_campaign_report_draft(
        scope=SCOPE, evidence={"posts": [("ev-posts", rows)]}, narrative=None
    )
    payload = build.payload
    CampaignReportV2.model_validate(payload)
    kols = payload["data"]["kol_contributions"]
    assert len(kols) == 20
    engagements = [row["engagement"] for row in kols]
    assert engagements == sorted(engagements, reverse=True)
    assert payload["data"]["overview"]["total_creators"] == 25


# ---------------------------------------------------------------------------
# 1b. 情感合计行防双计（真实 UAT 行形态回归）
# ---------------------------------------------------------------------------


def _real_sentiment_rows() -> list[dict[str, Any]]:
    """第三轮真实 UAT 的 query_analysis_data 情感 Evidence 行形态。

    具名平台明细行（短视频-抖音/小红书 × 正/中/负）+ 无平台键的跨平台合计行
    （声量恰为具名行之和）并存于同一 Evidence。
    """
    return [
        {"内容情感": "中性", "平台": "短视频-抖音", "声量": 101577, "互动数": 6518228},
        {"内容情感": "中性", "平台": "小红书", "声量": 59609, "互动数": 1033549},
        {"内容情感": "正面", "平台": "短视频-抖音", "声量": 95036, "互动数": 5978432},
        {"内容情感": "正面", "平台": "小红书", "声量": 20404, "互动数": 308520},
        {"内容情感": "负面", "平台": "小红书", "声量": 12341, "互动数": 520196},
        {"内容情感": "负面", "平台": "短视频-抖音", "声量": 6647, "互动数": 677708},
        {"内容情感": "中性", "声量": 161186, "互动数": 7551777},
        {"内容情感": "正面", "声量": 115440, "互动数": 6286952},
        {"内容情感": "负面", "声量": 18988, "互动数": 1197904},
    ]


def test_sentiment_aggregate_rows_not_double_counted() -> None:
    """具名平台行 + 合计行并存：summary 必须等于具名行之和（即合计行口径），
    不得把合计行再计入（真实 UAT 曾 2 倍双计）；by_platform 不出现 all 伪平台。"""
    build = build_campaign_report_draft(
        scope=SCOPE,
        evidence={
            "posts": [("ev-posts", _post_rows())],
            "sentiment": [("ev-sent", _real_sentiment_rows())],
        },
        narrative=None,
    )
    payload = build.payload
    CampaignReportV2.model_validate(payload)
    sentiment = payload["data"]["sentiment"]

    assert sentiment["summary"]["positive"]["count"] == 115440
    assert sentiment["summary"]["neutral"]["count"] == 161186
    assert sentiment["summary"]["negative"]["count"] == 18988

    by_platform = {row["platform"]: row for row in sentiment["by_platform"]}
    assert set(by_platform) == {"xiaohongshu", "douyin"}
    assert by_platform["xiaohongshu"]["positive"]["count"] == 20404
    assert by_platform["douyin"]["neutral"]["count"] == 101577
    # summary 恒等于各平台行之和。
    for name in ("positive", "neutral", "negative"):
        assert sentiment["summary"][name]["count"] == sum(
            row[name]["count"] for row in sentiment["by_platform"]
        )
    # share 按修正后总量归一。
    total = 115440 + 161186 + 18988
    assert sentiment["summary"]["positive"]["share"] == pytest.approx(
        round(115440 / total, 4)
    )


def test_sentiment_aggregate_only_rows_fall_back_to_all_platform() -> None:
    """上游只返回合计行（无平台拆分）时不丢数据：归入 all 平台，summary 即合计。"""
    aggregate_rows = [
        {"内容情感": "正面", "声量": 115440},
        {"内容情感": "中性", "声量": 161186},
        {"内容情感": "负面", "声量": 18988},
    ]
    build = build_campaign_report_draft(
        scope=SCOPE,
        evidence={
            "posts": [("ev-posts", _post_rows())],
            "sentiment": [("ev-sent", aggregate_rows)],
        },
        narrative=None,
    )
    sentiment = build.payload["data"]["sentiment"]
    assert sentiment["summary"]["positive"]["count"] == 115440
    assert [row["platform"] for row in sentiment["by_platform"]] == ["all"]
    assert sentiment["by_platform"][0]["neutral"]["count"] == 161186


# ---------------------------------------------------------------------------
# 2. restricted 路径
# ---------------------------------------------------------------------------


def test_missing_posts_evidence_produces_restricted() -> None:
    """posts Evidence 缺失 → overview/platform_contributions/timeline/top_posts
    unavailable，data_status=restricted 且 limitation 覆盖必需章节。"""
    evidence = {
        "sentiment": [
            ("ev-sent", [{"平台": "小红书", "情感": "正面", "声量": 10}])
        ]
    }
    build = build_campaign_report_draft(scope=SCOPE, evidence=evidence, narrative=None)
    payload = build.payload
    assert payload["data_status"] == "restricted"
    for section in ("overview", "platform_contributions", "timeline", "top_posts"):
        assert payload["availability"][section]["status"] == "unavailable"
        assert "no_evidence" in payload["availability"][section]["reason_codes"]
    assert payload["availability"]["sentiment"]["status"] == "complete"
    assert payload["data"]["overview"]["total_posts"] is None
    CampaignReportV2.model_validate(payload)


def test_missing_sentiment_produces_restricted() -> None:
    rows = [{**row, "情感": None} for row in _post_rows()]
    for row in rows:
        row.pop("情感")
    build = build_campaign_report_draft(
        scope=SCOPE, evidence={"posts": [("ev-posts", rows)]}, narrative=None
    )
    payload = build.payload
    assert payload["data_status"] == "restricted"
    assert payload["availability"]["sentiment"]["status"] == "unavailable"
    CampaignReportV2.model_validate(payload)


# ---------------------------------------------------------------------------
# 3. 输入契约
# ---------------------------------------------------------------------------


def test_invalid_narrative_supporting_path_raises_build_error() -> None:
    narrative = {
        "executive_summary": "结论",
        "phase_review": [],
        "findings": [
            {"title": "t", "detail": "d", "supporting_paths": ["data.nope.0.count"]}
        ],
        "recommendations": [],
    }
    with pytest.raises(DraftBuildError):
        build_campaign_report_draft(
            scope=SCOPE, evidence=_full_evidence(), narrative=narrative
        )


def test_no_evidence_at_all_raises_build_error() -> None:
    with pytest.raises(DraftBuildError):
        build_campaign_report_draft(scope=SCOPE, evidence={}, narrative=None)


def test_lineage_covers_all_required_numerics() -> None:
    build = build_campaign_report_draft(
        scope=SCOPE, evidence=_full_evidence(), narrative=NARRATIVE
    )
    required = required_numeric_pointers(build.payload)
    covered = {ref["artifact_path"] for ref in build.evidence_refs}
    assert required
    assert required <= covered


# ---------------------------------------------------------------------------
# 4. lineage DB freeze
# ---------------------------------------------------------------------------


async def test_lineage_freeze_passes_with_db_evidence(
    db_session, user_factory, session_factory, run_factory
) -> None:
    from app.agent_runtime.evidence import EvidenceWriter
    from app.agent_runtime.models import AgentRunAttempt, AgentStep, AgentToolCall

    user = await user_factory()
    session = await session_factory(user.id)
    run = await run_factory(session.id, user.id)
    now = datetime.now(UTC).replace(tzinfo=None)
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
    call = AgentToolCall(
        id=str(uuid4()),
        run_id=run.id,
        step_id=step.id,
        logical_call_id=f"call-{uuid4()}",
        service="mcp",
        internal_tool_name="query_raw_posts",
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
        session_id=session.id,
        run_id=run.id,
        tool_call_id=call.id,
        source_type="mcp",
        source_name="query_raw_posts",
        scope_json=None,
        period_json=None,
        raw_payload=_post_rows(),
    )

    build = build_campaign_report_draft(
        scope=SCOPE,
        evidence={"posts": [(item.id, _post_rows())]},
        narrative=NARRATIVE,
    )
    frozen = await validate_and_freeze_lineage(
        payload=build.payload,
        refs=build.evidence_refs,
        owner=LineageOwner(user_id=user.id, session_id=session.id),
        loader=DbLineageLoader(db_session),
    )
    assert frozen.refs
    overview_ref = next(
        ref
        for ref in frozen.refs
        if ref.artifact_path == "/data/overview/total_engagement"
    )
    assert {source.evidence_id for source in overview_ref.sources} == {item.id}
