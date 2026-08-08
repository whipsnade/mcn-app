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
        {"平台": "合计", "内容情感": "正面", "声量": 115440},
        {"平台": "合计", "内容情感": "中性", "声量": 161186},
        {"平台": "合计", "内容情感": "负面", "声量": 18988},
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
    # scope 配置小红书/抖音，但情感 Evidence 仅覆盖小红书；本轮 coverage
    # 规则要求该章节受限披露，不能把单平台样本伪装成完整跨平台结论。
    assert payload["availability"]["sentiment"]["status"] == "partial"
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


# ---------------------------------------------------------------------------
# Gate C Task 4：对比/归属/内部指标/ROI
# ---------------------------------------------------------------------------


def test_campaign_builder_merges_social_and_upload_evidence() -> None:
    """社媒指标以 DataTap 为主、成本/转化以 upload 为主；ROI 齐全时生成。"""
    evidence = {
        "posts": [("ev-posts", _post_rows())],
        "baseline": [("ev-baseline", [dict(_post_rows()[0], 互动数=80)])],
        "upload": [
            (
                "ev-upload",
                [
                    {
                        "平台": "合计",
                        "投放金额": 100000,
                        "曝光": 2000000,
                        "转化": 5000,
                        "销售额": 300000,
                    }
                ],
            )
        ],
    }
    scope = {**SCOPE, "attribution_rules": ["最后点击 7 天"]}
    build = build_campaign_report_draft(scope=scope, evidence=evidence)
    payload = build.payload
    CampaignReportV2.model_validate(payload)

    assert payload["data"]["internal_metrics"]["spend"] == 100000
    assert payload["data"]["internal_metrics"]["conversions"] == 5000
    assert payload["data"]["internal_metrics"]["cpc"] == 20
    # ROI 生成：spend + 转化/销售 + 归因窗口齐全。
    roi = payload["data"]["roi"]
    assert roi is not None
    assert roi["spend"] == 100000
    assert roi["roas"] == pytest.approx(3.0)
    assert roi["roi"] == pytest.approx(2.0)
    # 周期对比：baseline 有行 → current_baseline 系列存在。
    assert payload["data"]["comparisons"]["current_baseline"]
    assert payload["data"]["comparisons"]["current_baseline"][0]["metric"] == "volume"


def test_roi_section_is_absent_without_cost_and_conversion() -> None:
    """无成本/转化数据：roi=null，绝不估算或编造。"""
    evidence = {"posts": [("ev-posts", _post_rows())]}
    build = build_campaign_report_draft(scope=SCOPE, evidence=evidence)
    payload = build.payload
    CampaignReportV2.model_validate(payload)
    assert payload["data"]["roi"] is None
    assert payload["data"]["internal_metrics"] is None


def test_roi_absent_without_attribution_window() -> None:
    """有 spend 但无归因窗口：roi 仍为 null。"""
    evidence = {
        "posts": [("ev-posts", _post_rows())],
        "upload": [("ev-upload", [{"投放金额": 100000, "销售额": 300000}])],
    }
    build = build_campaign_report_draft(scope=SCOPE, evidence=evidence)
    payload = build.payload
    assert payload["data"]["internal_metrics"]["spend"] == 100000
    assert payload["data"]["roi"] is None


def test_campaign_builder_attribution_and_organic_summary() -> None:
    """归属：付费/自然/未知计数与占比；organic_summary 汇总自然传播。"""
    posts = _post_rows()
    posts[0]["归属"] = "付费商单"
    posts[1]["归属"] = "自然内容"
    evidence = {"posts": [("ev-posts", posts)]}
    build = build_campaign_report_draft(scope=SCOPE, evidence=evidence)
    payload = build.payload
    CampaignReportV2.model_validate(payload)
    attribution = payload["data"]["attribution"]
    assert attribution["paid_confirmed"] == 1
    assert attribution["organic"] == 1
    assert attribution["unknown"] == 1
    assert attribution["paid_confirmed_share"] == pytest.approx(1 / 3, abs=0.001)
    organic = payload["data"]["organic_summary"]
    # Gate C 审核：organic_summary 只统计确认自然行（1 行「自然内容」）。
    assert organic["posts"] == 1
    assert organic["engagement"] == 55


def test_campaign_builder_conflict_keeps_both_values_with_limitation() -> None:
    """社媒指标冲突：双值保留并生成 limitation，不静默覆盖。"""
    evidence = {
        "posts": [("ev-posts", _post_rows())],
        "upload": [
            (
                "ev-upload",
                [
                    {
                        "平台": "合计",
                        "投放金额": 100000,
                        "声量": 999999,
                        "互动数": 888888,
                        "销售额": 300000,
                    }
                ],
            )
        ],
    }
    build = build_campaign_report_draft(
        scope={**SCOPE, "attribution_rules": ["最后点击 7 天"]},
        evidence=evidence,
    )
    payload = build.payload
    assert payload["data_status"] == "restricted"
    conflicts = [limit for limit in payload["limitations"] if limit["code"] == "social_metric_conflict"]
    assert len(conflicts) == 1
    assert "DataTap 420" in conflicts[0]["message"]  # 互动冲突消息保留双值


# ---------------------------------------------------------------------------
# Gate C 审核修复：归因否定语义 / organic 只统计自然 / 去重 / 合计行优先
# ---------------------------------------------------------------------------


def test_attribution_negation_organic_before_paid() -> None:
    """「非商单」含「商单」、「unpaid」含「paid」：必须先判自然语义。"""
    posts = _post_rows()
    posts[0]["归属"] = "非商单"
    posts[1]["归属"] = "付费商单"
    # posts[2] 无归属字段 → unknown
    build = build_campaign_report_draft(scope=SCOPE, evidence={"posts": [("ev-1", posts)]})
    attribution = build.payload["data"]["attribution"]
    assert attribution["paid_confirmed"] == 1
    assert attribution["organic"] == 1
    assert attribution["unknown"] == 1


def test_attribution_unpaid_is_not_paid() -> None:
    posts = [_post_rows()[0]]
    posts[0]["归属"] = "unpaid"
    build = build_campaign_report_draft(scope=SCOPE, evidence={"posts": [("ev-1", posts)]})
    attribution = build.payload["data"]["attribution"]
    assert attribution["paid_confirmed"] == 0
    assert attribution["organic"] == 1


def test_attribution_negated_paid_phrase_is_organic() -> None:
    """「非付费」含子串「付费」，但语义是否定：绝不允许包含关系命中付费。"""
    posts = [_post_rows()[0]]
    posts[0]["归属"] = "非付费"
    build = build_campaign_report_draft(scope=SCOPE, evidence={"posts": [("ev-1", posts)]})
    attribution = build.payload["data"]["attribution"]
    assert attribution["paid_confirmed"] == 0
    assert attribution["organic"] == 1


def test_attribution_standardized_tokens_not_arbitrary_substring() -> None:
    """标准化 token 集合：付费/商单/广告命中付费；无关词不因包含子串误判。"""
    def _kind(value: str) -> str:
        posts = [_post_rows()[0]]
        posts[0]["归属"] = value
        build = build_campaign_report_draft(
            scope=SCOPE, evidence={"posts": [("ev-1", posts)]}
        )
        attribution = build.payload["data"]["attribution"]
        if attribution["paid_confirmed"] == 1:
            return "paid"
        if attribution["organic"] == 1:
            return "organic"
        return "unknown"

    assert _kind("付费") == "paid"
    assert _kind("商单") == "paid"
    assert _kind("付费商单") == "paid"
    assert _kind("自然") == "organic"
    assert _kind("自然内容") == "organic"
    assert _kind("非商单") == "organic"
    assert _kind("非付费") == "organic"
    assert _kind("unpaid") == "organic"
    # 无归属语义 → unknown，不得默认付费。
    assert _kind("其他") == "unknown"


def test_organic_summary_only_counts_organic_rows() -> None:
    """organic_summary 只聚合确认自然行；付费与未知不得进入自然指标。"""
    posts = _post_rows()
    posts[0]["归属"] = "付费商单"
    posts[1]["归属"] = "自然"
    posts[2]["归属"] = "organic"
    build = build_campaign_report_draft(scope=SCOPE, evidence={"posts": [("ev-1", posts)]})
    organic = build.payload["data"]["organic_summary"]
    assert organic["posts"] == 2  # 只有 2 行自然
    assert organic["engagement"] == 55 + 250  # 自然行互动


def test_duplicate_evidence_rows_not_double_counted() -> None:
    """同一 Evidence 行（同 evidence_id + source_path）合并去重，不重复计算。"""
    posts = _post_rows()
    evidence = {
        "posts": [("ev-1", posts)],
        "social": [("ev-1", posts)],  # 同 evidence 重复出现
    }
    build = build_campaign_report_draft(scope=SCOPE, evidence=evidence)
    payload = build.payload
    assert payload["data"]["overview"]["total_posts"] == 3  # 不是 6


def test_upload_detail_rows_aggregated_across_platforms() -> None:
    """多平台上传明细行正确汇总（无合计行时逐行聚合）。"""
    upload = [
        {"平台": "小红书", "投放金额": 40000, "曝光": 800000, "转化": 2000, "销售额": 120000},
        {"平台": "抖音", "投放金额": 60000, "曝光": 1200000, "转化": 3000, "销售额": 180000},
    ]
    build = build_campaign_report_draft(
        scope={**SCOPE, "attribution_rules": ["最后点击 7 天"]},
        evidence={"posts": [("ev-1", _post_rows())], "upload": [("ev-2", upload)]},
    )
    metrics = build.payload["data"]["internal_metrics"]
    assert metrics["spend"] == 100000
    assert metrics["impressions"] == 2000000
    assert metrics["conversions"] == 5000
    assert metrics["revenue"] == 300000


def test_upload_total_row_not_double_counted_with_details() -> None:
    """合计行与明细行同时存在：优先合计行，不重复累计。"""
    upload = [
        {"平台": "合计", "投放金额": 100000, "曝光": 2000000, "转化": 5000, "销售额": 300000},
        {"平台": "小红书", "投放金额": 40000, "曝光": 800000, "转化": 2000, "销售额": 120000},
        {"平台": "抖音", "投放金额": 60000, "曝光": 1200000, "转化": 3000, "销售额": 180000},
    ]
    build = build_campaign_report_draft(
        scope={**SCOPE, "attribution_rules": ["最后点击 7 天"]},
        evidence={"posts": [("ev-1", _post_rows())], "upload": [("ev-2", upload)]},
    )
    metrics = build.payload["data"]["internal_metrics"]
    assert metrics["spend"] == 100000  # 合计行，不是 200000
    assert metrics["impressions"] == 2000000


# ---------------------------------------------------------------------------
# Gate C 复审：Evidence 去重必须覆盖 attribution/organic/audience/comparison/lineage
# ---------------------------------------------------------------------------


def _dedup_rows() -> list[dict[str, Any]]:
    return [
        {
            "平台": "小红书",
            "帖子ID": "p1",
            "作者": "达人A",
            "用户ID": "u1",
            "发布时间": "2026-07-03 09:00:00",
            "声量": 1,
            "互动数": 100,
            "归属": "非商单",
            "地区": "上海",
        },
        {
            "平台": "小红书",
            "帖子ID": "p2",
            "作者": "达人B",
            "用户ID": "u2",
            "发布时间": "2026-07-04 09:00:00",
            "声量": 1,
            "互动数": 50,
            "归属": "付费商单",
            "地区": "北京",
        },
    ]


def test_dedup_covers_attribution_organic_audience_comparison_lineage() -> None:
    """同一 (evidence_id, source_path) 行被 posts/social/current 多分组引用时，
    attribution/organic/audience/comparison 只计一次，lineage 来源不重复。"""
    rows = _dedup_rows()
    evidence = {
        "posts": [("ev-1", rows)],
        "social": [("ev-1", rows)],
        "current": [("ev-1", rows)],
        "baseline": [("ev-baseline", [dict(rows[0], 互动数=10)])],
    }
    build = build_campaign_report_draft(scope=SCOPE, evidence=evidence)
    payload = build.payload
    CampaignReportV2.model_validate(payload)

    attribution = payload["data"]["attribution"]
    assert attribution["organic"] == 1
    assert attribution["paid_confirmed"] == 1
    assert attribution["unknown"] == 0

    organic = payload["data"]["organic_summary"]
    assert organic["posts"] == 1
    assert organic["engagement"] == 100

    regions = {r["region"]: r for r in payload["data"]["audience_regions"]}
    assert set(regions) == {"上海", "北京"}
    assert regions["上海"]["volume"] == 1
    assert regions["北京"]["volume"] == 1

    current_baseline = payload["data"]["comparisons"]["current_baseline"]
    posts_metric = next(s for s in current_baseline if s["metric"] == "posts")
    assert posts_metric["current"] == 2
    volume_metric = next(s for s in current_baseline if s["metric"] == "volume")
    assert volume_metric["current"] == 2

    refs = {ref["artifact_path"]: ref for ref in build.evidence_refs}
    organic_ref = refs["/data/organic_summary/posts"]
    sources = {(s["evidence_id"], s["source_path"]) for s in organic_ref["sources"]}
    assert sources == {("ev-1", "/0")}


# ---------------------------------------------------------------------------
# Gate C 审核修复：ROI 门禁（spend>0 + attribution_rules + 曝光 + 转化 + 收入）
# ---------------------------------------------------------------------------


def test_roi_null_with_comparison_mode_but_no_attribution_rules() -> None:
    """comparison_mode=mom 只是周期比较方式，绝不能作为归因窗口。"""
    evidence = {
        "posts": [("ev-1", _post_rows())],
        "upload": [("ev-2", [{"投放金额": 100000, "曝光": 2000000, "转化": 5000, "销售额": 300000}])],
    }
    build = build_campaign_report_draft(
        scope={**SCOPE, "comparison_mode": "mom"},
        evidence=evidence,
    )
    assert build.payload["data"]["roi"] is None


def test_roi_null_when_impressions_missing() -> None:
    evidence = {
        "posts": [("ev-1", _post_rows())],
        "upload": [("ev-2", [{"投放金额": 100000, "转化": 5000, "销售额": 300000}])],
    }
    build = build_campaign_report_draft(
        scope={**SCOPE, "attribution_rules": ["最后点击 7 天"]},
        evidence=evidence,
    )
    assert build.payload["data"]["roi"] is None


def test_roi_null_when_conversions_missing() -> None:
    evidence = {
        "posts": [("ev-1", _post_rows())],
        "upload": [("ev-2", [{"投放金额": 100000, "曝光": 2000000, "销售额": 300000}])],
    }
    build = build_campaign_report_draft(
        scope={**SCOPE, "attribution_rules": ["最后点击 7 天"]},
        evidence=evidence,
    )
    assert build.payload["data"]["roi"] is None


def test_roi_null_when_revenue_missing() -> None:
    evidence = {
        "posts": [("ev-1", _post_rows())],
        "upload": [("ev-2", [{"投放金额": 100000, "曝光": 2000000, "转化": 5000}])],
    }
    build = build_campaign_report_draft(
        scope={**SCOPE, "attribution_rules": ["最后点击 7 天"]},
        evidence=evidence,
    )
    assert build.payload["data"]["roi"] is None


def test_roi_null_and_no_crash_when_spend_zero() -> None:
    evidence = {
        "posts": [("ev-1", _post_rows())],
        "upload": [("ev-2", [{"投放金额": 0, "曝光": 2000000, "转化": 5000, "销售额": 300000}])],
    }
    build = build_campaign_report_draft(
        scope={**SCOPE, "attribution_rules": ["最后点击 7 天"]},
        evidence=evidence,
    )
    assert build.payload["data"]["roi"] is None


def test_roi_generated_only_with_all_conditions() -> None:
    evidence = {
        "posts": [("ev-1", _post_rows())],
        "upload": [("ev-2", [{"投放金额": 100000, "曝光": 2000000, "转化": 5000, "销售额": 300000}])],
    }
    build = build_campaign_report_draft(
        scope={**SCOPE, "attribution_rules": ["最后点击 7 天"]},
        evidence=evidence,
    )
    roi = build.payload["data"]["roi"]
    assert roi is not None
    assert roi["attribution_window"] == "最后点击 7 天"
    assert roi["roas"] == pytest.approx(3.0)
    assert roi["roi"] == pytest.approx(2.0)


def test_conversions_zero_is_valid_data_not_missing() -> None:
    """conversions=0 是有效数据：保留为 0；CPC 无定义置 None；ROI/ROAS 与 CPC 分别处理。"""
    evidence = {
        "posts": [("ev-1", _post_rows())],
        "upload": [("ev-2", [{"投放金额": 100000, "曝光": 2000000, "转化": 0, "销售额": 300000}])],
    }
    build = build_campaign_report_draft(
        scope={**SCOPE, "attribution_rules": ["最后点击 7 天"]},
        evidence=evidence,
    )
    metrics = build.payload["data"]["internal_metrics"]
    # 真实 0 保留为 0，绝不退化为 None/缺失。
    assert metrics["conversions"] == 0
    # CPC = spend/conversions：conversions=0 无定义 → None，但这是真实 0 而非缺失。
    assert metrics["cpc"] is None
    # CPM 与 conversions 无关，正常计算。
    assert metrics["cpm"] == pytest.approx(50.0)
    # ROI/ROAS 只依赖 spend+revenue+归因窗口（+曝光齐全），conversions=0 不阻断。
    roi = build.payload["data"]["roi"]
    assert roi is not None
    assert roi["conversions"] == 0
    assert roi["roas"] == pytest.approx(3.0)
    assert roi["roi"] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Gate C 第三轮：缺失与真实零治理（observed 显式跟踪，0 不是 None）
#
# 规则：字段出现且合计为 0 → 保留 0；字段完全没出现 → None。数值为 0 时
# 同样必须登记 lineage（禁止 truthiness 判断）。organic_summary /
# audience_regions / _group_totals（comparison）/ social conflict detection
# 共用同一规则。
# ---------------------------------------------------------------------------


_MISSING = object()


def _minimal_row(
    *,
    pid: str,
    volume: Any = _MISSING,
    engagement: Any = _MISSING,
    author: Any = _MISSING,
    **extra: Any,
) -> dict[str, Any]:
    """只带平台/帖子身份的可控行；默认不带任何数值/作者字段。"""
    row: dict[str, Any] = {
        "平台": "小红书",
        "帖子ID": pid,
        "发布时间": "2026-07-03 09:00:00",
    }
    if volume is not _MISSING:
        row["声量"] = volume
    if engagement is not _MISSING:
        row["互动数"] = engagement
    if author is not _MISSING:
        row["作者"] = author
    row.update(extra)
    return row


def _refs(rows: list[dict[str, Any]]):
    from app.agent_artifacts.builders.raw_rows import RowRef

    return [RowRef("ev-1", f"/{index}", row) for index, row in enumerate(rows)]


def test_group_totals_missing_vs_real_zero() -> None:
    """_group_totals：字段出现合计 0 → 保留 0；完全没出现 → None。"""
    from app.agent_artifacts.builders.campaign import _group_totals

    only_zero = _group_totals(_refs([_minimal_row(pid="p1", volume=0, engagement=0)]))
    assert only_zero["volume"] == 0
    assert only_zero["engagement"] == 0
    assert only_zero["posts"] == 1
    assert only_zero["creators"] is None  # 无作者字段 → 未观测

    missing = _group_totals(_refs([_minimal_row(pid="p1", author="a"), _minimal_row(pid="p2", author="b")]))
    assert missing["volume"] is None  # 声量字段完全没出现
    assert missing["engagement"] is None  # 互动字段完全没出现
    assert missing["posts"] == 2
    assert missing["creators"] == 2


def test_comparison_missing_metric_is_none_not_zero() -> None:
    """baseline 行没有声量/互动/作者字段：comparison 系列必须是 None，不是 0。"""
    current = [_minimal_row(pid="p1", volume=2, engagement=10, author="a")]
    baseline = [_minimal_row(pid="b1")]  # 只有帖子身份，无任何数值字段
    build = build_campaign_report_draft(
        scope=SCOPE,
        evidence={"posts": [("ev-current", current)], "baseline": [("ev-baseline", baseline)]},
    )
    payload = build.payload
    CampaignReportV2.model_validate(payload)
    series = {s["metric"]: s for s in payload["data"]["comparisons"]["current_baseline"]}
    assert series["volume"]["current"] == 2
    assert series["volume"]["baseline"] is None
    assert series["volume"]["delta"] is None
    assert series["volume"]["rate"] is None
    assert series["engagement"]["baseline"] is None
    assert series["creators"]["baseline"] is None


def test_comparison_real_zero_kept_and_lineaged() -> None:
    """baseline 声量字段出现但合计 0：baseline=0 保留，且 lineage 必须登记 0 值。"""
    current = [_minimal_row(pid="p1", volume=2, engagement=10, author="a")]
    baseline = [_minimal_row(pid="b1", volume=0, engagement=0, author="b")]
    build = build_campaign_report_draft(
        scope=SCOPE,
        evidence={"posts": [("ev-current", current)], "baseline": [("ev-baseline", baseline)]},
    )
    series = {s["metric"]: s for s in build.payload["data"]["comparisons"]["current_baseline"]}
    assert series["volume"]["baseline"] == 0
    assert series["engagement"]["baseline"] == 0
    refs = {ref["artifact_path"] for ref in build.evidence_refs}
    assert "/data/comparisons/current_baseline/0/baseline" in refs
    assert "/data/comparisons/current_baseline/1/baseline" in refs
    required = required_numeric_pointers(build.payload)
    assert required <= refs


def test_organic_summary_zero_volume_kept_and_lineaged() -> None:
    """organic 行声量字段出现但合计 0：volume=0 保留且 lineage 登记；
    声量字段完全没出现 → volume=None（不因 or 0 伪造 0，也不因 truthiness 丢 lineage）。"""
    posts = [
        {**_minimal_row(pid="p1", volume=0, engagement=0, author="a"), "归属": "自然"},
        {**_minimal_row(pid="p2", volume=0, engagement=0, author="b"), "归属": "自然"},
    ]
    build = build_campaign_report_draft(scope=SCOPE, evidence={"posts": [("ev-1", posts)]})
    organic = build.payload["data"]["organic_summary"]
    assert organic["volume"] == 0
    assert organic["engagement"] == 0
    refs = {ref["artifact_path"] for ref in build.evidence_refs}
    assert "/data/organic_summary/volume" in refs
    assert "/data/organic_summary/engagement" in refs

    missing = build_campaign_report_draft(
        scope=SCOPE,
        evidence={"posts": [("ev-1", [{**_minimal_row(pid="p1", author="a"), "归属": "自然"}])]},
    )
    organic = missing.payload["data"]["organic_summary"]
    assert organic["volume"] is None
    assert organic["engagement"] is None


def test_audience_regions_zero_total_no_fake_share() -> None:
    """地域声量字段出现但全部为 0：volume=0 保留；总声量为 0 时 share=None，绝不伪造 share=0。"""
    posts = [
        {**_minimal_row(pid="p1", volume=0, author="a"), "地区": "上海"},
        {**_minimal_row(pid="p2", volume=0, author="b"), "地区": "北京"},
    ]
    build = build_campaign_report_draft(scope=SCOPE, evidence={"posts": [("ev-1", posts)]})
    regions = {r["region"]: r for r in build.payload["data"]["audience_regions"]}
    assert regions["上海"]["volume"] == 0
    assert regions["北京"]["volume"] == 0
    assert regions["上海"]["share"] is None
    assert regions["北京"]["share"] is None
    CampaignReportV2.model_validate(build.payload)


def test_audience_regions_missing_volume_is_none() -> None:
    """地域行没有声量字段：volume=None、share=None，不得伪造 0。"""
    posts = [
        {**_minimal_row(pid="p1", author="a"), "地区": "上海"},
        {**_minimal_row(pid="p2", author="b"), "地区": "北京"},
    ]
    build = build_campaign_report_draft(scope=SCOPE, evidence={"posts": [("ev-1", posts)]})
    regions = {r["region"]: r for r in build.payload["data"]["audience_regions"]}
    assert regions["上海"]["volume"] is None
    assert regions["上海"]["share"] is None
    CampaignReportV2.model_validate(build.payload)


def test_social_conflict_uses_observed_volume() -> None:
    """social 行声量字段出现但合计 0（观测值）vs upload 999 → 真实冲突保留双值；
    social 行无声量字段（未观测）→ 不产生声量冲突。"""
    observed_zero = [
        {**_minimal_row(pid="p1", volume=0, author="a")},
        {**_minimal_row(pid="p2", volume=0, author="b")},
    ]
    build = build_campaign_report_draft(
        scope=SCOPE,
        evidence={
            "posts": [("ev-1", observed_zero)],
            "upload": [("ev-2", [{"平台": "合计", "声量": 999, "投放金额": 100}])],
        },
    )
    conflicts = [lim for lim in build.payload["limitations"] if lim["code"] == "social_metric_conflict"]
    assert any("声量" in lim["message"] for lim in conflicts)

    not_observed = [_minimal_row(pid="p1", author="a")]
    build = build_campaign_report_draft(
        scope=SCOPE,
        evidence={
            "posts": [("ev-1", not_observed)],
            "upload": [("ev-2", [{"平台": "合计", "声量": 999, "投放金额": 100}])],
        },
    )
    conflicts = [lim for lim in build.payload["limitations"] if lim["code"] == "social_metric_conflict"]
    assert not any("声量" in lim["message"] for lim in conflicts)


def test_overview_zero_engagement_kept_and_lineaged() -> None:
    """overview：互动字段出现但合计 0 → total_engagement=0 保留且 lineage 登记。"""
    posts = [_minimal_row(pid="p1", engagement=0, author="a"), _minimal_row(pid="p2", engagement=0, author="b")]
    build = build_campaign_report_draft(scope=SCOPE, evidence={"posts": [("ev-1", posts)]})
    overview = build.payload["data"]["overview"]
    assert overview["total_engagement"] == 0
    refs = {ref["artifact_path"] for ref in build.evidence_refs}
    assert "/data/overview/total_engagement" in refs


# ---------------------------------------------------------------------------
# Gate C 第三轮：活动归属字段语义
#
# 「是否付费」是布尔语义字段：值直接表达 是/否、true/false、1/0、yes/no；
# 「归属/投放类型/付费自然/attribution」继续用标准化文本 token。归属判定
# 必须保留命中的字段名，非付费/非商单/unpaid 优先归 organic，未知值保持
# unknown（绝不默认付费）。
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("是", "paid_confirmed"),
        ("true", "paid_confirmed"),
        ("TRUE", "paid_confirmed"),
        ("1", "paid_confirmed"),
        ("yes", "paid_confirmed"),
        ("否", "organic"),
        ("false", "organic"),
        ("0", "organic"),
        ("no", "organic"),
    ],
)
def test_attribution_boolean_field_value_mapping(value: str, expected: str) -> None:
    """「是否付费」布尔字段：是/否、true/false、1/0、yes/no 精确映射。"""
    from app.agent_artifacts.builders.campaign import _attribution_kind

    ref = _refs([{"是否付费": value}])[0]
    kind, field = _attribution_kind(ref)
    assert kind == expected
    assert field == "是否付费"


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("归属", "付费商单", "paid_confirmed"),
        ("归属", "自然", "organic"),
        ("归属", "非付费", "organic"),
        ("归属", "非商单", "organic"),
        ("归属", "unpaid", "organic"),
        ("归属", "其他", "unknown"),
        ("投放类型", "广告投放", "paid_confirmed"),
        ("投放类型", "非投放", "organic"),
        ("付费/自然", "自然流量", "organic"),
        ("attribution", "paid", "paid_confirmed"),
        ("attribution", "sponsored", "paid_confirmed"),
    ],
)
def test_attribution_text_field_token_mapping(field: str, value: str, expected: str) -> None:
    """文本语义字段（归属/投放类型/付费自然/attribution）：标准化 token 匹配，
    且返回命中的字段名。"""
    from app.agent_artifacts.builders.campaign import _attribution_kind

    ref = _refs([{field: value}])[0]
    kind, matched = _attribution_kind(ref)
    assert kind == expected
    assert matched == field


def test_attribution_unknown_and_missing_never_default_paid() -> None:
    """未知值保持 unknown、缺字段 unknown；绝不默认付费。"""
    from app.agent_artifacts.builders.campaign import _attribution_kind

    assert _attribution_kind(_refs([{"归属": "不清楚"}])[0]) == ("unknown", "归属")
    assert _attribution_kind(_refs([{"是否付费": "也许"}])[0]) == ("unknown", "是否付费")
    assert _attribution_kind(_refs([{"平台": "小红书"}])[0]) == ("unknown", None)


def test_attribution_boolean_field_counts_in_builder() -> None:
    """builder 集成：「是否付费」= 是/否/true 正确进入 paid/organic 计数。"""
    posts = [
        {**_minimal_row(pid="p1", engagement=10, author="a"), "是否付费": "是"},
        {**_minimal_row(pid="p2", engagement=20, author="b"), "是否付费": "否"},
        {**_minimal_row(pid="p3", engagement=30, author="c"), "是否付费": "true"},
    ]
    build = build_campaign_report_draft(scope=SCOPE, evidence={"posts": [("ev-1", posts)]})
    attribution = build.payload["data"]["attribution"]
    assert attribution["paid_confirmed"] == 2
    assert attribution["organic"] == 1
    assert attribution["unknown"] == 0


def test_attribution_boolean_organic_feeds_organic_summary() -> None:
    """「是否付费」= 否 的行计入 organic_summary（布尔字段与自然聚合联动）。"""
    posts = [
        {**_minimal_row(pid="p1", volume=5, engagement=10, author="a"), "是否付费": "否"},
        {**_minimal_row(pid="p2", volume=3, engagement=20, author="b"), "是否付费": "是"},
    ]
    build = build_campaign_report_draft(scope=SCOPE, evidence={"posts": [("ev-1", posts)]})
    organic = build.payload["data"]["organic_summary"]
    assert organic["posts"] == 1
    assert organic["volume"] == 5
    assert organic["engagement"] == 10


# ---------------------------------------------------------------------------
# Gate C 第三轮 P1-2：活动对比指标 lineage 来源精确归期
#
# current_baseline/current_post 的 current/baseline/delta/rate 必须引用各自的
# 期别行集合：current 只引用 current_rows；baseline 按系列引用 baseline_rows
# （current_baseline）或 post_rows（current_post）；delta/rate 同时引用两期；
# sources 按 (evidence_id, source_path) 去重保序；真实 0 仍登记 lineage，
# None 不登记；三组 Evidence 不得混淆。
# ---------------------------------------------------------------------------


def _comparison_evidence() -> dict[str, list[tuple[str, Any]]]:
    """三组不同 evidence_id 的对比行：ev-current / ev-baseline / ev-post。"""
    return {
        "current": [("ev-current", [_minimal_row(pid="c1", volume=10, engagement=100, author="a")])],
        "baseline": [("ev-baseline", [_minimal_row(pid="b1", volume=8, engagement=80, author="b")])],
        "post": [("ev-post", [_minimal_row(pid="p1", volume=12, engagement=120, author="c")])],
    }


def _comparison_ref(build, series: str, metric_index: int, field: str) -> dict[str, Any]:
    artifact_path = f"/data/comparisons/{series}/{metric_index}/{field}"
    refs = [ref for ref in build.evidence_refs if ref["artifact_path"] == artifact_path]
    assert len(refs) == 1, artifact_path
    return refs[0]


def test_comparison_lineage_current_only_from_current_evidence() -> None:
    """current 值只引用 ev-current；baseline 只引用 ev-baseline；绝不混入对方。"""
    build = build_campaign_report_draft(scope=SCOPE, evidence=_comparison_evidence())
    CampaignReportV2.model_validate(build.payload)

    current_ref = _comparison_ref(build, "current_baseline", 0, "current")
    assert [s["evidence_id"] for s in current_ref["sources"]] == ["ev-current"]
    assert [s["source_path"] for s in current_ref["sources"]] == ["/0"]

    baseline_ref = _comparison_ref(build, "current_baseline", 0, "baseline")
    assert [s["evidence_id"] for s in baseline_ref["sources"]] == ["ev-baseline"]


def test_comparison_lineage_delta_rate_cover_both_periods() -> None:
    """delta/rate 同时包含正确的两期 evidence_id，source_path 正确、无重复。"""
    build = build_campaign_report_draft(scope=SCOPE, evidence=_comparison_evidence())
    CampaignReportV2.model_validate(build.payload)

    for field in ("delta", "rate"):
        ref = _comparison_ref(build, "current_baseline", 0, field)
        pairs = [(s["evidence_id"], s["source_path"]) for s in ref["sources"]]
        assert pairs == [("ev-current", "/0"), ("ev-baseline", "/0")]
        assert len(pairs) == len(set(pairs))  # 无重复


def test_comparison_lineage_current_post_baseline_from_post_evidence() -> None:
    """current_post 系列的 baseline 字段装的是 post 期值：只引用 ev-post；
    delta/rate 引用 ev-current + ev-post。"""
    build = build_campaign_report_draft(scope=SCOPE, evidence=_comparison_evidence())
    CampaignReportV2.model_validate(build.payload)

    post_value_ref = _comparison_ref(build, "current_post", 0, "baseline")
    assert [s["evidence_id"] for s in post_value_ref["sources"]] == ["ev-post"]

    current_ref = _comparison_ref(build, "current_post", 0, "current")
    assert [s["evidence_id"] for s in current_ref["sources"]] == ["ev-current"]

    delta_ref = _comparison_ref(build, "current_post", 0, "delta")
    pairs = [(s["evidence_id"], s["source_path"]) for s in delta_ref["sources"]]
    assert pairs == [("ev-current", "/0"), ("ev-post", "/0")]


def test_comparison_lineage_all_metrics_keep_period_ownership() -> None:
    """四个指标（volume/engagement/posts/creators）的 current 都只引用 ev-current，
    baseline 都只引用 ev-baseline（任何指标都不混入对方 Evidence）。"""
    build = build_campaign_report_draft(scope=SCOPE, evidence=_comparison_evidence())
    CampaignReportV2.model_validate(build.payload)

    for index in range(4):
        current_ref = _comparison_ref(build, "current_baseline", index, "current")
        assert {s["evidence_id"] for s in current_ref["sources"]} == {"ev-current"}
        baseline_ref = _comparison_ref(build, "current_baseline", index, "baseline")
        assert {s["evidence_id"] for s in baseline_ref["sources"]} == {"ev-baseline"}


def test_comparison_zero_baseline_kept_and_lineaged() -> None:
    """baseline=0（真实 0）：baseline=0 保留、lineage 登记；delta 引用两期。"""
    evidence = {
        "current": [("ev-current", [_minimal_row(pid="c1", volume=10, author="a")])],
        "baseline": [("ev-baseline", [_minimal_row(pid="b1", volume=0, author="b")])],
    }
    build = build_campaign_report_draft(scope=SCOPE, evidence=evidence)
    CampaignReportV2.model_validate(build.payload)
    series = {s["metric"]: s for s in build.payload["data"]["comparisons"]["current_baseline"]}
    assert series["volume"]["baseline"] == 0

    baseline_ref = _comparison_ref(build, "current_baseline", 0, "baseline")
    assert [s["evidence_id"] for s in baseline_ref["sources"]] == ["ev-baseline"]
    delta_ref = _comparison_ref(build, "current_baseline", 0, "delta")
    assert [s["evidence_id"] for s in delta_ref["sources"]] == ["ev-current", "ev-baseline"]
    # baseline=0 时 rate 无定义（不登记 lineage），required 覆盖仍完整。
    required = required_numeric_pointers(build.payload)
    covered = {ref["artifact_path"] for ref in build.evidence_refs}
    assert required <= covered
