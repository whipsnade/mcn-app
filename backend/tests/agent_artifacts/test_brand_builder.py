"""brand_report_v3 Draft builder tests（设计 §12.1 / v3 加固 §3.3/§6.1，B2）。

覆盖：
1. 确定性聚合口径（移植自旧 brand_assembler）：overview 汇总/合计行跳过、
   情感计数与占比、日趋势、话题、热帖、环比/同比对比窗与 delta/rate；
2. restricted 路径：必需章节 Evidence 缺失 → partial/unavailable + limitation；
3. 模型叙事透传与 supporting_paths 校验失败 → DraftBuildError；
4. lineage：字段级 Evidence 引用覆盖全部必选 numeric，DB freeze 校验通过；
5. 输入契约：无任何可用 Evidence → DraftBuildError（结构化回喂模型）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

import pytest

from app.agent_artifacts.builders.brand import (
    SCHEMA_VERSION,
    build_brand_report_draft,
    comparison_windows,
)
from app.agent_artifacts.builders.common import DraftBuildError
from app.agent_artifacts.lineage import (
    DbLineageLoader,
    LineageOwner,
    required_numeric_pointers,
    validate_and_freeze_lineage,
)
from app.agent_artifacts.payloads.brand import BrandReportV3

SCOPE = {
    "brand": "瑞幸咖啡",
    "period": {"start": "2026-07-01", "end": "2026-07-31", "timezone": "Asia/Shanghai"},
    "platforms": ["xiaohongshu", "douyin"],
    "keywords": ["瑞幸"],
    "comparison_mode": "mom",
}

NARRATIVE = {
    "executive_summary": "瑞幸咖啡本期声量与互动均环比增长。",
    "findings": [
        {
            "title": "抖音贡献主要声量",
            "detail": "抖音声量 200，占比约三分之二。",
            "supporting_paths": ["data.overview.platforms.1.volume"],
        }
    ],
    "recommendations": [
        {
            "title": "加码抖音投放",
            "action": "提高抖音平台预算占比",
            "rationale": "抖音声量与互动领先",
            "supporting_paths": ["data.overview.platforms.1.engagement"],
        }
    ],
}


def _overview_rows() -> list[dict[str, Any]]:
    return [
        {
            "平台": "小红书",
            "声量": 100,
            "互动数": 1000,
            "发帖数": 80,
            "正面声量": 60,
            "中性声量": 30,
            "负面声量": 10,
        },
        {
            "平台": "抖音",
            "声量": 200,
            "互动数": 3000,
            "发帖数": 150,
            "正面声量": 120,
            "中性声量": 60,
            "负面声量": 20,
        },
        # DataTap 常在明细后附合计行：存在具名平台行时必须跳过，防双计。
        {"平台": "合计", "声量": 300, "互动数": 4000, "发帖数": 230},
    ]


def _mom_rows() -> list[dict[str, Any]]:
    return [
        {"平台": "小红书", "声量": 80, "互动数": 800, "发帖数": 60},
        {"平台": "抖音", "声量": 150, "互动数": 2000, "发帖数": 100},
    ]


def _yoy_rows() -> list[dict[str, Any]]:
    return [
        {"平台": "小红书", "声量": 50, "互动数": 500, "发帖数": 40},
        {"平台": "抖音", "声量": 100, "互动数": 1000, "发帖数": 90},
    ]


def _sentiment_rows() -> list[dict[str, Any]]:
    return [
        {"平台": "小红书", "情感": "正面", "声量": 60},
        {"平台": "小红书", "情感": "中性", "声量": 30},
        {"平台": "小红书", "情感": "负面", "声量": 10},
        {"平台": "抖音", "情感": "正面", "声量": 120},
        {"平台": "抖音", "情感": "中性", "声量": 60},
        {"平台": "抖音", "情感": "负面", "声量": 20},
    ]


def _trend_rows() -> list[dict[str, Any]]:
    return [
        {
            "日期": "2026-07-01",
            "平台": "小红书",
            "声量": 10,
            "互动数": 100,
            "正面": 6,
            "中性": 3,
            "负面": 1,
        },
        {
            "日期": "2026-07-01",
            "平台": "抖音",
            "声量": 20,
            "互动数": 200,
            "正面": 12,
            "中性": 6,
            "负面": 2,
        },
        {
            "日期": "2026-07-02",
            "平台": "小红书",
            "声量": 15,
            "互动数": 150,
            "正面": 9,
            "中性": 4,
            "负面": 2,
        },
    ]


def _topic_rows() -> list[dict[str, Any]]:
    return [
        {"话题": "生椰拿铁", "声量": 50, "互动数": 500, "正面": 40, "中性": 5, "负面": 5},
        {"话题": "酱香拿铁", "声量": 30, "互动数": 200, "正面": 10, "中性": 10, "负面": 10},
    ]


def _post_rows() -> list[dict[str, Any]]:
    return [
        {
            "平台": "小红书",
            "帖子ID": "p1",
            "标题": "生椰拿铁测评",
            "作者": "达人A",
            "发布时间": "2026-07-05 10:00:00",
            "点赞数": 100,
            "评论数": 20,
            "分享数": 5,
            "互动数": 125,
            "帖子链接": "https://example.com/p1",
        },
        {
            "平台": "抖音",
            "帖子ID": "p2",
            "标题": "新品开箱",
            "作者": "达人B",
            "发布时间": "2026-07-06 12:00:00",
            "点赞数": 300,
            "评论数": 40,
            "分享数": 10,
            "互动数": 350,
            "帖子链接": "https://example.com/p2",
        },
    ]


def _full_evidence() -> dict[str, list[tuple[str, Any]]]:
    return {
        "overview_current": [("ev-ov", _overview_rows())],
        "overview_mom": [("ev-mom", _mom_rows())],
        "sentiment": [("ev-sent", _sentiment_rows())],
        "daily_trend": [("ev-trend", _trend_rows())],
        "topics": [("ev-topics", _topic_rows())],
        "top_posts": [("ev-posts", _post_rows())],
    }


# ---------------------------------------------------------------------------
# 1. 确定性聚合口径
# ---------------------------------------------------------------------------


def test_complete_payload_deterministic_aggregation() -> None:
    build = build_brand_report_draft(
        scope=SCOPE, evidence=_full_evidence(), narrative=NARRATIVE
    )
    payload = build.payload
    BrandReportV3.model_validate(payload)

    assert build.module == "brand"
    assert build.schema_version == SCHEMA_VERSION == "brand_report_v3"
    assert build.artifact_type == SCHEMA_VERSION
    assert build.business_fields == {"brand": "瑞幸咖啡"}
    assert payload["data_status"] == "complete"

    overview = payload["data"]["overview"]
    assert overview["total_volume"] == 300
    assert overview["total_engagement"] == 4000
    assert overview["total_posts"] == 230
    # 净情感指数：(180 - 30) / 300 * 100。
    assert overview["sentiment_score"] == 50.0
    platforms = overview["platforms"]
    assert [row["platform"] for row in platforms] == ["xiaohongshu", "douyin"]
    assert platforms[0]["volume"] == 100
    assert platforms[0]["engagement"] == 1000
    assert platforms[0]["posts"] == 80
    assert platforms[0]["share_of_voice"] == pytest.approx(round(100 / 300, 4))
    assert platforms[0]["sentiment_score"] == 50.0
    assert platforms[1]["share_of_voice"] == pytest.approx(round(200 / 300, 4))

    sentiment = payload["data"]["sentiment"]
    assert sentiment["summary"]["positive"] == {"count": 180, "share": 0.6}
    assert sentiment["summary"]["neutral"] == {"count": 90, "share": 0.3}
    assert sentiment["summary"]["negative"] == {"count": 30, "share": 0.1}
    by_platform = {row["platform"]: row for row in sentiment["by_platform"]}
    assert by_platform["xiaohongshu"]["positive"] == {"count": 60, "share": 0.6}
    assert by_platform["douyin"]["negative"] == {"count": 20, "share": 0.1}

    trend = payload["data"]["daily_trend"]
    assert [item["date"] for item in trend] == [
        "2026-07-01",
        "2026-07-01",
        "2026-07-02",
    ]
    assert trend[0]["platform"] == "xiaohongshu"
    assert trend[0]["volume"] == 10
    assert trend[0]["engagement"] == 100
    assert trend[0]["positive"] == 6
    assert trend[1]["platform"] == "douyin"

    topics = payload["data"]["topics"]
    assert [item["topic"] for item in topics] == ["生椰拿铁", "酱香拿铁"]
    assert topics[0]["volume"] == 50
    assert topics[0]["engagement"] == 500
    # (40 - 5) / 50 * 100。
    assert topics[0]["sentiment_score"] == 70.0

    top_posts = payload["data"]["top_posts"]
    assert [post["post_id"] for post in top_posts] == ["p2", "p1"]
    assert top_posts[0]["engagement"] == 350
    assert top_posts[0]["likes"] == 300
    assert top_posts[0]["url"] == "https://example.com/p2"
    assert top_posts[0]["author"] == "达人B"

    # 模型叙事原样透传。
    assert payload["narrative"]["executive_summary"] == NARRATIVE["executive_summary"]
    assert payload["narrative"]["findings"][0]["title"] == "抖音贡献主要声量"


def test_daily_trend_accepts_chinese_day_column() -> None:
    trend_rows = [
        {
            "日": "2026-07-01",
            "平台": "小红书",
            "声量": 10,
            "互动数": 100,
            "正面": 6,
            "中性": 3,
            "负面": 1,
        },
    ]
    evidence = {
        "overview_current": [("ev-ov", _overview_rows())],
        "overview_mom": [("ev-mom", _mom_rows())],
        "sentiment": [("ev-sent", _sentiment_rows())],
        "daily_trend": [("ev-trend", trend_rows)],
        "topics": [("ev-topics", _topic_rows())],
        "top_posts": [("ev-posts", _post_rows())],
    }
    build = build_brand_report_draft(
        scope=SCOPE, evidence=evidence, narrative=NARRATIVE
    )
    trend = build.payload["data"]["daily_trend"]
    assert [item["date"] for item in trend] == ["2026-07-01"]
    assert trend[0]["volume"] == 10


def test_comparison_mom_windows_and_rates() -> None:
    build = build_brand_report_draft(
        scope=SCOPE, evidence=_full_evidence(), narrative=NARRATIVE
    )
    comparisons = build.payload["data"]["comparisons"]

    mom = comparisons["mom"]
    assert mom["status"] == "complete"
    assert mom["baseline_period"]["start"] == "2026-05-31"
    assert mom["baseline_period"]["end"] == "2026-06-30"
    metrics = {metric["metric"]: metric for metric in mom["metrics"]}
    volume = metrics["total_volume"]
    assert volume["current"] == 300
    assert volume["baseline"] == 230
    assert volume["delta"] == 70
    assert volume["rate"] == pytest.approx(round(70 / 230, 4))
    engagement = metrics["total_engagement"]
    assert engagement["delta"] == 1200
    assert engagement["rate"] == pytest.approx(round(1200 / 2800, 4))
    posts = metrics["total_posts"]
    assert posts["current"] == 230
    assert posts["baseline"] == 160

    yoy = comparisons["yoy"]
    assert yoy["status"] == "not_requested"
    assert yoy["baseline_period"] is None
    assert yoy["metrics"] == []


def test_comparison_mom_yoy_includes_yoy_window() -> None:
    scope = {**SCOPE, "comparison_mode": "mom_yoy"}
    evidence = _full_evidence()
    evidence["overview_yoy"] = [("ev-yoy", _yoy_rows())]
    build = build_brand_report_draft(scope=scope, evidence=evidence, narrative=NARRATIVE)
    yoy = build.payload["data"]["comparisons"]["yoy"]
    assert yoy["status"] == "complete"
    assert yoy["baseline_period"]["start"] == "2025-07-01"
    assert yoy["baseline_period"]["end"] == "2025-07-31"
    metrics = {metric["metric"]: metric for metric in yoy["metrics"]}
    assert metrics["total_volume"]["baseline"] == 150
    assert metrics["total_volume"]["delta"] == 150
    assert metrics["total_volume"]["rate"] == 1.0


def test_comparison_none_marks_both_not_requested() -> None:
    scope = {**SCOPE, "comparison_mode": "none"}
    build = build_brand_report_draft(scope=scope, evidence=_full_evidence(), narrative=NARRATIVE)
    comparisons = build.payload["data"]["comparisons"]
    for kind in ("mom", "yoy"):
        assert comparisons[kind]["status"] == "not_requested"
        assert comparisons[kind]["metrics"] == []
    BrandReportV3.model_validate(build.payload)


def test_comparison_requested_but_missing_baseline_is_unavailable() -> None:
    evidence = _full_evidence()
    del evidence["overview_mom"]
    build = build_brand_report_draft(scope=SCOPE, evidence=evidence, narrative=NARRATIVE)
    mom = build.payload["data"]["comparisons"]["mom"]
    assert mom["status"] == "unavailable"
    # 请求的对比期必须有 baseline_period，即使数据缺失。
    assert mom["baseline_period"]["start"] == "2026-05-31"
    BrandReportV3.model_validate(build.payload)


def test_comparison_windows_leap_year_shift() -> None:
    windows = comparison_windows(
        {"start": "2024-02-29", "end": "2024-03-01", "timezone": "Asia/Shanghai"}, "mom_yoy"
    )
    assert windows["mom"] == (date(2024, 2, 27), date(2024, 2, 28))
    # 2/29 向前平移一年 → 2/28。
    assert windows["yoy"] == (date(2023, 2, 28), date(2023, 3, 1))


def test_aggregate_only_overview_rows_are_used() -> None:
    """上游只返回合计行（无平台明细）时，聚合行归入 all 平台作为唯一数据。"""
    evidence = _full_evidence()
    evidence["overview_current"] = [
        ("ev-ov", [{"声量": 300, "互动数": 4000, "发帖数": 230}])
    ]
    # 单平台聚合行时 platforms.1 不存在，共用叙事不引用它。
    build = build_brand_report_draft(scope=SCOPE, evidence=evidence, narrative=None)
    overview = build.payload["data"]["overview"]
    assert overview["total_volume"] == 300
    assert [row["platform"] for row in overview["platforms"]] == ["all"]
    BrandReportV3.model_validate(build.payload)


def test_sentiment_falls_back_to_overview_split() -> None:
    """无情感明细 Evidence 时，用 overview 汇总的正/中/负构成兜底（partial 披露）。"""
    evidence = _full_evidence()
    del evidence["sentiment"]
    build = build_brand_report_draft(scope=SCOPE, evidence=evidence, narrative=NARRATIVE)
    payload = build.payload
    sentiment = payload["data"]["sentiment"]
    assert sentiment["summary"]["positive"] == {"count": 180, "share": 0.6}
    assert sentiment["summary"]["negative"] == {"count": 30, "share": 0.1}
    assert sentiment["by_platform"] == []
    assert payload["availability"]["sentiment"]["status"] == "partial"
    codes = [item["code"] for item in payload["limitations"]]
    assert "sentiment_from_overview" in codes
    BrandReportV3.model_validate(payload)


def test_sentiment_aggregate_rows_not_double_counted() -> None:
    """真实 UAT 行形态：具名平台行 + 无平台键合计行并存时 summary 不得双计。

    brand 与 campaign 共用 build_sentiment_section；此处锁定 brand 侧口径：
    summary 等于具名平台行之和，by_platform 不出现 all 伪平台，平台
    sentiment_score 与 overview.sentiment_score 同步按修正后口径计算。
    """
    evidence = _full_evidence()
    evidence["sentiment"] = [
        (
            "ev-sent",
            [
                {"内容情感": "中性", "平台": "短视频-抖音", "声量": 101577},
                {"内容情感": "中性", "平台": "小红书", "声量": 59609},
                {"内容情感": "正面", "平台": "短视频-抖音", "声量": 95036},
                {"内容情感": "正面", "平台": "小红书", "声量": 20404},
                {"内容情感": "负面", "平台": "小红书", "声量": 12341},
                {"内容情感": "负面", "平台": "短视频-抖音", "声量": 6647},
                # 无平台键的跨平台合计行（恰为具名行之和）。
                {"内容情感": "中性", "声量": 161186},
                {"内容情感": "正面", "声量": 115440},
                {"内容情感": "负面", "声量": 18988},
            ],
        )
    ]
    build = build_brand_report_draft(scope=SCOPE, evidence=evidence, narrative=None)
    payload = build.payload
    BrandReportV3.model_validate(payload)
    sentiment = payload["data"]["sentiment"]

    assert sentiment["summary"]["positive"]["count"] == 115440
    assert sentiment["summary"]["neutral"]["count"] == 161186
    assert sentiment["summary"]["negative"]["count"] == 18988
    assert {row["platform"] for row in sentiment["by_platform"]} == {
        "xiaohongshu",
        "douyin",
    }
    # 净情感指数按修正后 summary：(115440 - 18988) / 295614 * 100 ≈ 32.63。
    assert payload["data"]["overview"]["sentiment_score"] == pytest.approx(
        round((115440 - 18988) / 295614 * 100, 2)
    )


# ---------------------------------------------------------------------------
# 2. restricted 路径
# ---------------------------------------------------------------------------


def test_missing_required_section_produces_restricted() -> None:
    """topics Evidence 缺失 → topics unavailable + limitation，data_status=restricted。"""
    evidence = _full_evidence()
    del evidence["topics"]
    build = build_brand_report_draft(scope=SCOPE, evidence=evidence, narrative=NARRATIVE)
    payload = build.payload
    assert payload["data_status"] == "restricted"
    assert payload["availability"]["topics"]["status"] == "unavailable"
    assert "no_evidence" in payload["availability"]["topics"]["reason_codes"]
    assert payload["data"]["topics"] == []
    assert any(
        item["code"] == "no_evidence" and "topics" in item["affected_paths"]
        for item in payload["limitations"]
    )
    BrandReportV3.model_validate(payload)


def test_missing_sentiment_and_topics_both_disclosed() -> None:
    evidence = _full_evidence()
    del evidence["sentiment"]
    del evidence["topics"]
    # overview 行不含情感构成 → sentiment 完全不可用（单平台，叙事从缺省生成）。
    evidence["overview_current"] = [
        ("ev-ov", [{"平台": "小红书", "声量": 100, "互动数": 1000, "发帖数": 80}])
    ]
    build = build_brand_report_draft(scope=SCOPE, evidence=evidence, narrative=None)
    payload = build.payload
    assert payload["data_status"] == "restricted"
    assert payload["availability"]["sentiment"]["status"] == "unavailable"
    # 情感缺失 → overview.sentiment_score / 平台 sentiment_score 为 null → overview partial。
    assert payload["availability"]["overview"]["status"] == "partial"
    assert payload["data"]["overview"]["sentiment_score"] is None
    BrandReportV3.model_validate(payload)


def test_optional_sections_absent_keep_complete_status() -> None:
    """非必需章节（content_types/creator_tiers/organic_vs_paid/regions）无 Evidence
    不影响 data_status=complete。"""
    build = build_brand_report_draft(
        scope=SCOPE, evidence=_full_evidence(), narrative=NARRATIVE
    )
    payload = build.payload
    assert payload["data_status"] == "complete"
    for section in ("content_types", "creator_tiers", "organic_vs_paid", "regions"):
        assert payload["availability"][section]["status"] == "unavailable"
        assert payload["data"][section] == []
    BrandReportV3.model_validate(payload)


# ---------------------------------------------------------------------------
# 3. 叙事与输入契约
# ---------------------------------------------------------------------------


def test_invalid_narrative_supporting_path_raises_build_error() -> None:
    narrative = {
        "executive_summary": "结论",
        "findings": [
            {"title": "t", "detail": "d", "supporting_paths": ["data.overview.not_a_field"]}
        ],
        "recommendations": [],
    }
    with pytest.raises(DraftBuildError):
        build_brand_report_draft(scope=SCOPE, evidence=_full_evidence(), narrative=narrative)


def test_no_evidence_at_all_raises_build_error() -> None:
    with pytest.raises(DraftBuildError):
        build_brand_report_draft(scope=SCOPE, evidence={}, narrative=NARRATIVE)
    with pytest.raises(DraftBuildError):
        build_brand_report_draft(
            scope=SCOPE, evidence={"overview_current": []}, narrative=NARRATIVE
        )


def test_invalid_scope_raises_build_error() -> None:
    bad_scope = {**SCOPE, "comparison_mode": "weekly"}
    with pytest.raises(DraftBuildError):
        build_brand_report_draft(scope=bad_scope, evidence=_full_evidence())


def test_default_narrative_is_generated_when_omitted() -> None:
    build = build_brand_report_draft(scope=SCOPE, evidence=_full_evidence())
    narrative = build.payload["narrative"]
    assert narrative["executive_summary"]
    BrandReportV3.model_validate(build.payload)


def test_lineage_covers_all_required_numerics() -> None:
    build = build_brand_report_draft(
        scope=SCOPE, evidence=_full_evidence(), narrative=NARRATIVE
    )
    required = required_numeric_pointers(build.payload)
    covered = {ref["artifact_path"] for ref in build.evidence_refs}
    assert required <= covered
    assert required  # 防空调试通过


# ---------------------------------------------------------------------------
# 4. lineage DB freeze（Evidence 归属 + 指针可解析）
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

    async def _write_evidence(payload: Any) -> str:
        call = AgentToolCall(
            id=str(uuid4()),
            run_id=run.id,
            step_id=step.id,
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
            session_id=session.id,
            run_id=run.id,
            tool_call_id=call.id,
            source_type="mcp",
            source_name="query_analysis_data",
            scope_json=None,
            period_json=None,
            raw_payload=payload,
        )
        return item.id

    groups = {
        "overview_current": _overview_rows(),
        "overview_mom": _mom_rows(),
        "sentiment": _sentiment_rows(),
        "daily_trend": _trend_rows(),
        "topics": _topic_rows(),
        "top_posts": _post_rows(),
    }
    evidence_input: dict[str, list[tuple[str, Any]]] = {}
    evidence_ids: dict[str, str] = {}
    for group, payload in groups.items():
        evidence_id = await _write_evidence(payload)
        evidence_input[group] = [(evidence_id, payload)]
        evidence_ids[group] = evidence_id

    build = build_brand_report_draft(
        scope=SCOPE, evidence=evidence_input, narrative=NARRATIVE
    )
    frozen = await validate_and_freeze_lineage(
        payload=build.payload,
        refs=build.evidence_refs,
        owner=LineageOwner(user_id=user.id, session_id=session.id),
        loader=DbLineageLoader(db_session),
    )
    assert frozen.refs
    ov_ref = next(
        ref for ref in frozen.refs if ref.artifact_path == "/data/overview/total_volume"
    )
    assert {source.evidence_id for source in ov_ref.sources} == {
        evidence_ids["overview_current"]
    }
    mom_ref = next(
        ref
        for ref in frozen.refs
        if ref.artifact_path == "/data/comparisons/mom/metrics/0/baseline"
    )
    assert {source.evidence_id for source in mom_ref.sources} == {
        evidence_ids["overview_mom"]
    }
