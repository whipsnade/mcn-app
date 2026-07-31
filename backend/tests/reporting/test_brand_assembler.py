"""brand_report_v2 数据组装器测试：手工构造 agent_trajectory plan_json。

工具输出形状参照 docs/datatap-mcp-tools.md 简化构造（DataTap structured_content
统一为 {"result": "<json string>"} 包装）；全程不调真实 MCP/模型。
"""

from __future__ import annotations

from datetime import date
import json

import pytest

from app.goals.schemas import GoalPeriod
from app.reporting.brand_assembler import assemble_brand_report, comparison_windows
from app.reporting.brand_payload import BrandReportPayload


TOOL_TAG = "datatap.insight.match.best.tag.v1"
TOOL_OVERVIEW = "datatap.insight.social.statistic.overview.v1"
TOOL_TREND = "datatap.insight.social.statistic.trend.v1"
TOOL_ANALYSIS = "datatap.insight.query.analysis.v1"
TOOL_USER_PROFILE = "datatap.insight.social.statistic.user.profile.v1"
TOOL_RAW_POSTS = "datatap.insight.query.raw.posts.v1"

PARAMS = {
    "brand": "肯德基",
    "period": {"start": "2026-06-01", "end": "2026-06-30"},
    "platforms": ["小红书", "抖音"],
    "comparison_mode": "mom_yoy",
}
# 当期 2026-06-01~2026-06-30（30 天）；环比 2026-05-02~2026-05-31；同比 2025-06-01~2025-06-30。
MOM_WINDOW = ("2026-05-02", "2026-05-31")
YOY_WINDOW = ("2025-06-01", "2025-06-30")


def _summary(rows: object) -> dict[str, str]:
    """DataTap structured_content 包装：result 为 JSON 字符串。"""
    return {"result": json.dumps(rows, ensure_ascii=False)}


def _step(
    step_id: str,
    tool: str,
    *,
    start: str | None = None,
    end: str | None = None,
    goal: str = "",
) -> dict[str, object]:
    arguments: dict[str, str] = {}
    if start is not None:
        arguments["start_time"] = start
    if end is not None:
        arguments["end_time"] = end
    return {
        "id": step_id,
        "internal_tool_name": tool,
        "arguments": arguments,
        "evidence_goal": goal,
    }


def _note(
    step_id: str, tool: str, summary: object, *, status: str = "settled"
) -> dict[str, object]:
    return {"step_id": step_id, "tool": tool, "status": status, "summary": summary}


def _plan(steps: list[dict], notes: list[dict]) -> dict[str, object]:
    return {"schema": "agent_trajectory_v1", "steps": steps, "results": notes}


def _overview_rows(mentions: tuple[int, int], *, with_sentiment: bool = False) -> list[dict]:
    rows = [
        {"平台": "小红书", "声量": mentions[0], "曝光量": 50000, "互动数": 8000},
        {"平台": "抖音", "声量": mentions[1], "曝光量": 90000, "互动数": 12000},
    ]
    if with_sentiment:
        rows[0].update({"正面声量": 600, "中性声量": 300, "负面声量": 100})
        rows[1].update({"正面声量": 1200, "中性声量": 600, "负面声量": 200})
    return rows


def _trend_rows(last_day: int = 30) -> list[dict]:
    rows = []
    for day in range(1, last_day + 1):
        date_text = f"2026-06-{day:02d}"
        rows.append({"日期": date_text, "平台": "小红书", "声量": 30 + day, "互动数": 100 + day})
        rows.append({"日期": date_text, "平台": "抖音", "声量": 50 + day, "互动数": 200 + day})
    return rows


def _sentiment_rows() -> list[dict]:
    return [
        {"平台": "小红书", "情感": "正面", "声量": 600, "互动数": 4000},
        {"平台": "小红书", "情感": "中性", "声量": 300, "互动数": 2000},
        {"平台": "小红书", "情感": "负面", "声量": 100, "互动数": 500},
        {"平台": "抖音", "情感": "正面", "声量": 1200, "互动数": 6000},
        {"平台": "抖音", "情感": "中性", "声量": 600, "互动数": 3000},
        {"平台": "抖音", "情感": "负面", "声量": 200, "互动数": 1000},
    ]


def _region_rows(count: int) -> list[dict]:
    return [
        {"地区": f"省份{i}", "声量": 1000 - i * 30, "互动数": 500 - i * 10}
        for i in range(count)
    ]


def _post_rows() -> list[dict]:
    rows = []
    for i in range(17):
        rows.append(
            {
                "平台": "小红书",
                "帖子ID": f"xhs-{i}",
                "标题": None if i == 3 else f"肯德基新品测评 {i}",
                "昵称": f"美食博主{i}",
                "互动数": 1000 - i * 10,
                "阅读数": 5000 - i * 100,
                "点赞数": 500 - i * 5,
                "评论数": 100 - i,
                "收藏数": 50 - i,
                "转发数": 20 - i,
                "情感": "正面",
                "粉丝数": 1_500_000 if i == 0 else 50_000,
                "发布时间": "2026-06-15",
                "帖子链接": (
                    "https://www.xiaohongshu.com/explore/abc" if i == 0 else "javascript:bad"
                ),
            }
        )
    # 显式标注非品牌相关：即使互动量最高也必须剔除。
    rows.append({"平台": "小红书", "标题": "无关内容", "互动数": 999999, "品牌相关": "否"})
    for i in range(3):
        rows.append(
            {
                "平台": "抖音",
                "作品ID": f"dy-{i}",
                "标题": f"肯德基抖音视频 {i}",
                "作者": f"抖音达人{i}",
                "互动数": 300 - i * 50,
                "播放数": 8000 - i * 1000,
                "点赞数": 200 - i * 20,
                "评论数": 40 - i * 5,
                "分享数": 30 - i * 3,
                "内容情感": "中性",
            }
        )
    return rows


def _complete_plan() -> dict[str, object]:
    steps = [
        _step("step_1", TOOL_TAG, goal="current: 品牌标签匹配"),
        _step(
            "step_2",
            TOOL_OVERVIEW,
            start="2026-06-01",
            end="2026-06-30",
            goal="current: 当期声量概览",
        ),
        _step("step_3", TOOL_OVERVIEW, start=MOM_WINDOW[0], end=MOM_WINDOW[1], goal="mom: 环比概览"),
        _step("step_4", TOOL_OVERVIEW, start=YOY_WINDOW[0], end=YOY_WINDOW[1], goal="yoy: 同比概览"),
        _step(
            "step_5",
            TOOL_TREND,
            start="2026-06-01",
            end="2026-06-30",
            goal="current: 当期日趋势",
        ),
        _step(
            "step_6",
            TOOL_ANALYSIS,
            start="2026-06-01",
            end="2026-06-30",
            goal="current: 平台情感分布",
        ),
        _step(
            "step_7",
            TOOL_USER_PROFILE,
            start="2026-06-01",
            end="2026-06-30",
            goal="current: 受众地域画像",
        ),
        _step(
            "step_8",
            TOOL_ANALYSIS,
            start="2026-06-01",
            end="2026-06-30",
            goal="current: 内容类型分布",
        ),
        _step(
            "step_9",
            TOOL_ANALYSIS,
            start="2026-06-01",
            end="2026-06-30",
            goal="current: 达人层级分布",
        ),
        _step(
            "step_10",
            TOOL_ANALYSIS,
            start="2026-06-01",
            end="2026-06-30",
            goal="current: 自然与商单构成",
        ),
        _step(
            "step_11",
            TOOL_RAW_POSTS,
            start="2026-06-01",
            end="2026-06-30",
            goal="current: 品牌热门原帖",
        ),
    ]
    notes = [
        _note("step_1", TOOL_TAG, _summary([{"标签名称": "肯德基"}])),
        _note("step_2", TOOL_OVERVIEW, _summary(_overview_rows((1000, 2000), with_sentiment=True))),
        _note("step_3", TOOL_OVERVIEW, _summary([{"平台": "小红书", "声量": 500},
                                                 {"平台": "抖音", "声量": 1000}])),
        _note("step_4", TOOL_OVERVIEW, _summary([{"平台": "小红书", "声量": 400},
                                                 {"平台": "抖音", "声量": 600}])),
        _note("step_5", TOOL_TREND, _summary(_trend_rows())),
        _note("step_6", TOOL_ANALYSIS, _summary(_sentiment_rows())),
        _note("step_7", TOOL_USER_PROFILE, _summary(_region_rows(5))),
        _note("step_8", TOOL_ANALYSIS, _summary([{"内容类型": "图文", "声量": 700},
                                                 {"内容类型": "视频", "声量": 300}])),
        _note("step_9", TOOL_ANALYSIS, _summary([{"达人层级": "头部达人", "声量": 500},
                                                 {"达人层级": "腰部达人", "声量": 300},
                                                 {"达人层级": "尾部达人", "声量": 200}])),
        _note("step_10", TOOL_ANALYSIS, _summary([{"是否商单": "否", "声量": 800},
                                                  {"是否商单": "是", "声量": 200}])),
        _note("step_11", TOOL_RAW_POSTS, _summary(_post_rows())),
    ]
    return _plan(steps, notes)


def test_complete_evidence_assembles_full_payload() -> None:
    payload = assemble_brand_report(_complete_plan(), PARAMS)

    assert payload.template_version == "brand_report_v2"
    assert payload.data_status == "complete"
    assert set(payload.availability) == {
        "overview",
        "sentiment",
        "daily_trend",
        "content_creators",
        "regions",
        "top_posts",
        "insights",
        "methodology",
    }
    assert all(chapter.status == "complete" for chapter in payload.availability.values())

    # scope 与 query_spec
    assert payload.scope.brand == "肯德基"
    assert payload.scope.period_start == "2026-06-01"
    assert payload.scope.period_end == "2026-06-30"
    assert payload.scope.comparison_mode == "mom_yoy"
    assert payload.scope.data_as_of is None  # 趋势覆盖到 period.end
    assert payload.query_spec.original_term == "肯德基"
    assert payload.query_spec.matched_tag == "肯德基"
    assert payload.query_spec.fallback_keyword is None
    assert MOM_WINDOW[0] in payload.query_spec.comparison_definition
    assert YOY_WINDOW[0] in payload.query_spec.comparison_definition

    # 综合概览：平台指标与合计
    platforms = {row.platform: row for row in payload.data.overview.platforms}
    assert platforms["xiaohongshu"].mentions == 1000
    assert platforms["douyin"].interactions == 12000
    totals = payload.data.overview.total_mentions
    assert totals.current == 3000
    assert totals.mom.status == "ok" and totals.mom.value == 1500
    assert totals.yoy.status == "ok" and totals.yoy.value == 1000
    # 环比/同比百分比由组装器按 data 计算：(3000-1500)/1500、(3000-1000)/1000
    assert totals.mom_change_pct == 100.0
    assert totals.yoy_change_pct == 200.0
    assert payload.data.overview.total_interactions.current == 20000
    split = payload.data.overview.sentiment_split
    assert (split.positive, split.neutral, split.negative) == (1800, 900, 300)

    # 情感分析：占比按平台内合计计算
    xhs_positive = next(
        row
        for row in payload.data.sentiment.rows
        if row.platform == "xiaohongshu" and row.sentiment == "正面"
    )
    assert xhs_positive.mentions == 600
    assert xhs_positive.share_pct == 60.0

    # 日趋势：按日期聚合升序，峰值取声量最大日
    points = payload.data.daily_trend.points
    assert len(points) == 30
    assert points[0].date == "2026-06-01"
    assert points[-1].date == "2026-06-30"
    assert points[0].mentions == 82  # 31 + 51
    assert payload.data.daily_trend.peak_date == "2026-06-30"

    # 内容类型 / 达人层级 / 自然商单
    content = {row.content_type: row for row in payload.data.content_types}
    assert content["图文"].share_pct == 70.0
    tiers = {row.tier: row for row in payload.data.creator_tiers}
    assert tiers["头部达人"].share_pct == 50.0
    organic = payload.data.organic_vs_paid
    assert organic.organic_mentions == 800
    assert organic.organic_share_pct == 80.0
    assert organic.paid_share_pct == 20.0

    # 热帖：小红书截断 15 条、互动量降序、非品牌相关剔除
    xhs_posts = [row for row in payload.data.top_posts if row.platform == "xiaohongshu"]
    douyin_posts = [row for row in payload.data.top_posts if row.platform == "douyin"]
    assert len(xhs_posts) == 15
    assert len(douyin_posts) == 3
    assert xhs_posts[0].interactions == 1000
    assert all(
        xhs_posts[i].interactions >= xhs_posts[i + 1].interactions
        for i in range(len(xhs_posts) - 1)
    )
    assert all(row.title != "无关内容" for row in xhs_posts)
    # 缺失字段保留 null（不填 0、不拼 URL）；非法 URL → null
    assert xhs_posts[3].title is None
    assert xhs_posts[0].url == "https://www.xiaohongshu.com/explore/abc"
    assert xhs_posts[1].url is None
    assert xhs_posts[0].creator_tier == "头部达人"
    assert xhs_posts[1].creator_tier == "尾部达人"
    assert douyin_posts[0].exposure_count == 8000
    assert douyin_posts[0].share_count == 30

    # sources 覆盖全部 settled 调用
    assert len(payload.sources) == 11
    assert {entry.tool for entry in payload.sources} >= {TOOL_OVERVIEW, TOOL_RAW_POSTS}
    assert all(entry.step_id for entry in payload.sources)


def test_payload_json_roundtrip() -> None:
    payload = assemble_brand_report(_complete_plan(), PARAMS)
    restored = BrandReportPayload.model_validate(payload.model_dump(mode="json"))
    assert restored == payload


def test_comparison_windows_mom_and_mom_yoy() -> None:
    period = GoalPeriod(start="2026-06-01", end="2026-06-30")
    windows = comparison_windows(period, "mom_yoy")
    assert windows["mom"] == (date(2026, 5, 2), date(2026, 5, 31))
    assert windows["yoy"] == (date(2025, 6, 1), date(2025, 6, 30))


def test_comparison_windows_mom_only_has_no_yoy() -> None:
    period = GoalPeriod(start="2026-06-01", end="2026-06-30")
    windows = comparison_windows(period, "mom")
    assert "mom" in windows
    assert "yoy" not in windows


def test_comparison_windows_leap_day_shifts_to_feb_28() -> None:
    period = GoalPeriod(start="2024-02-01", end="2024-02-29")
    windows = comparison_windows(period, "mom_yoy")
    assert windows["yoy"] == (date(2023, 2, 1), date(2023, 2, 28))
    # mom 窗为紧邻上一等长周期（29 天）
    assert windows["mom"] == (date(2024, 1, 3), date(2024, 1, 31))


def _minimal_params(mode: str = "mom") -> dict[str, object]:
    return {
        "brand": "肯德基",
        "period": {"start": "2026-06-01", "end": "2026-06-30"},
        "platforms": ["小红书"],
        "comparison_mode": mode,
    }


def test_period_kind_prefix_wins_over_arguments() -> None:
    # evidence_goal 前缀优先：mom: 标注 + 落在 mom 窗的日期 → 计入环比。
    steps = [
        _step("step_1", TOOL_OVERVIEW, start="2026-06-01", end="2026-06-30", goal="current: 当期"),
        _step("step_2", TOOL_OVERVIEW, start=MOM_WINDOW[0], end=MOM_WINDOW[1], goal="mom: 上期"),
        # 前缀 mom 但日期不在任何窗内：前缀仍优先。
        _step("step_3", TOOL_OVERVIEW, start="2026-01-01", end="2026-01-31", goal="mom: 上期补充"),
    ]
    notes = [
        _note("step_1", TOOL_OVERVIEW, _summary([{"平台": "小红书", "声量": 900}])),
        _note("step_2", TOOL_OVERVIEW, _summary([{"平台": "小红书", "声量": 300}])),
        _note("step_3", TOOL_OVERVIEW, _summary([{"平台": "小红书", "声量": 150}])),
    ]
    payload = assemble_brand_report(_plan(steps, notes), _minimal_params())
    totals = payload.data.overview.total_mentions
    assert totals.current == 900
    assert totals.mom.value == 450  # step_2 + step_3 都按前缀计入环比


def test_period_kind_date_match_fallback_without_prefix() -> None:
    # 无前缀：arguments 日期与对比窗精确匹配兜底；都不匹配按 current。
    steps = [
        _step("step_1", TOOL_OVERVIEW, start="2026-06-01", end="2026-06-30"),
        _step("step_2", TOOL_OVERVIEW, start=MOM_WINDOW[0], end=MOM_WINDOW[1]),
        _step("step_3", TOOL_OVERVIEW, start="2026-06-01", end="2026-06-15"),
    ]
    notes = [
        _note("step_1", TOOL_OVERVIEW, _summary([{"平台": "小红书", "声量": 900}])),
        _note("step_2", TOOL_OVERVIEW, _summary([{"平台": "小红书", "声量": 300}])),
        _note("step_3", TOOL_OVERVIEW, _summary([{"平台": "小红书", "声量": 100}])),
    ]
    payload = assemble_brand_report(_plan(steps, notes), _minimal_params())
    totals = payload.data.overview.total_mentions
    assert totals.mom.value == 300
    assert totals.current == 1000  # step_3 判不出期别按 current 并入


def test_mom_mode_yoy_always_not_requested() -> None:
    steps = [
        _step("step_1", TOOL_OVERVIEW, start="2026-06-01", end="2026-06-30", goal="current: 当期"),
        _step("step_2", TOOL_OVERVIEW, start=YOY_WINDOW[0], end=YOY_WINDOW[1], goal="yoy: 同比"),
    ]
    notes = [
        _note("step_1", TOOL_OVERVIEW, _summary([{"平台": "小红书", "声量": 900}])),
        _note("step_2", TOOL_OVERVIEW, _summary([{"平台": "小红书", "声量": 500}])),
    ]
    payload = assemble_brand_report(_plan(steps, notes), _minimal_params(mode="mom"))
    yoy = payload.data.overview.total_mentions.yoy
    assert yoy.status == "not_requested"
    assert yoy.value is None
    assert payload.data.overview.total_mentions.yoy_change_pct is None


def test_missing_period_marks_comparison_restricted() -> None:
    params = {"brand": "肯德基", "platforms": ["小红书"], "comparison_mode": "mom_yoy"}
    steps = [_step("step_1", TOOL_OVERVIEW, goal="current: 当期概览")]
    notes = [_note("step_1", TOOL_OVERVIEW, _summary([{"平台": "小红书", "声量": 900}]))]
    payload = assemble_brand_report(_plan(steps, notes), params)
    totals = payload.data.overview.total_mentions
    assert totals.mom.status == "restricted" and totals.mom.reason == "invalid_period"
    assert totals.yoy.status == "restricted" and totals.yoy.reason == "invalid_period"
    assert payload.scope.period_start is None
    assert "无有效" in payload.query_spec.comparison_definition


def test_mom_mode_without_period_yoy_not_requested() -> None:
    params = {"brand": "肯德基", "platforms": ["小红书"], "comparison_mode": "mom"}
    steps = [_step("step_1", TOOL_OVERVIEW, goal="current: 当期概览")]
    notes = [_note("step_1", TOOL_OVERVIEW, _summary([{"平台": "小红书", "声量": 900}]))]
    payload = assemble_brand_report(_plan(steps, notes), params)
    totals = payload.data.overview.total_mentions
    assert totals.mom.status == "restricted" and totals.mom.reason == "invalid_period"
    assert totals.yoy.status == "not_requested"


def test_failed_comparison_evidence_marks_tool_failed() -> None:
    steps = [
        _step("step_1", TOOL_OVERVIEW, start="2026-06-01", end="2026-06-30", goal="current: 当期"),
        _step("step_2", TOOL_OVERVIEW, start=MOM_WINDOW[0], end=MOM_WINDOW[1], goal="mom: 上期"),
    ]
    notes = [
        _note("step_1", TOOL_OVERVIEW, _summary([{"平台": "小红书", "声量": 900}])),
        _note("step_2", TOOL_OVERVIEW, "调用失败（upstream_error）", status="failed"),
    ]
    payload = assemble_brand_report(_plan(steps, notes), _minimal_params())
    mom = payload.data.overview.total_mentions.mom
    assert mom.status == "restricted" and mom.reason == "tool_failed"


def test_missing_comparison_evidence_marks_no_data() -> None:
    steps = [_step("step_1", TOOL_OVERVIEW, start="2026-06-01", end="2026-06-30")]
    notes = [_note("step_1", TOOL_OVERVIEW, _summary([{"平台": "小红书", "声量": 900}]))]
    payload = assemble_brand_report(_plan(steps, notes), _minimal_params())
    mom = payload.data.overview.total_mentions.mom
    assert mom.status == "restricted" and mom.reason == "no_data"


def test_top_posts_truncated_per_platform_and_regions_capped() -> None:
    plan = _complete_plan()
    # 替换地域证据为 25 行（>20 截断）
    notes = plan["results"]
    notes[6] = _note("step_7", TOOL_USER_PROFILE, _summary(_region_rows(25)))
    payload = assemble_brand_report(plan, PARAMS)
    assert len(payload.data.regions) == 20
    assert payload.data.regions[0].region == "省份0"
    assert payload.data.regions[0].mentions == 1000
    assert payload.data.regions[0].share_pct == pytest.approx(1000 / 16000 * 100, abs=0.01)
    mentions = [row.mentions for row in payload.data.regions]
    assert mentions == sorted(mentions, reverse=True)


def test_trend_gap_writes_data_as_of() -> None:
    plan = _complete_plan()
    plan["results"][4] = _note("step_5", TOOL_TREND, _summary(_trend_rows(last_day=28)))
    payload = assemble_brand_report(plan, PARAMS)
    assert payload.scope.data_as_of == "2026-06-28"
    assert payload.availability["daily_trend"].status == "partial"
    assert payload.data_status == "partial"


def test_no_overview_evidence_raises_lookup_error() -> None:
    steps = [
        _step("step_1", TOOL_TREND, start="2026-06-01", end="2026-06-30", goal="current: 趋势"),
    ]
    notes = [_note("step_1", TOOL_TREND, _summary(_trend_rows()))]
    with pytest.raises(LookupError, match="no_evidence_collected"):
        assemble_brand_report(_plan(steps, notes), PARAMS)
    with pytest.raises(LookupError, match="no_evidence_collected"):
        assemble_brand_report(None, PARAMS)


def test_missing_dimension_chapter_unavailable_and_warning_merged() -> None:
    plan = _complete_plan()
    # 移除趋势证据（step_5）
    plan["steps"] = [step for step in plan["steps"] if step["id"] != "step_5"]
    plan["results"] = [note for note in plan["results"] if note["step_id"] != "step_5"]
    payload = assemble_brand_report(
        plan, PARAMS, warning_code="brand_trend_data_unavailable"
    )
    chapter = payload.availability["daily_trend"]
    assert chapter.status == "unavailable"
    assert chapter.reason is not None
    # warning 人话化：reason 写入人话说明而非裸 code。
    assert "趋势数据未成功获取" in chapter.reason
    assert "brand_trend_data_unavailable" not in chapter.reason
    assert chapter.missing_fields == []
    assert chapter.source_tools == []
    assert chapter.collected_at is None
    assert payload.data.daily_trend.points == []
    assert payload.data_status == "partial"
    # 其他章节不受影响
    assert payload.availability["overview"].status == "complete"
    assert payload.availability["methodology"].status == "complete"


def test_missing_sentiment_degrades_insights() -> None:
    plan = _complete_plan()
    plan["steps"] = [step for step in plan["steps"] if step["id"] != "step_6"]
    plan["results"] = [note for note in plan["results"] if note["step_id"] != "step_6"]
    payload = assemble_brand_report(plan, PARAMS)
    assert payload.availability["sentiment"].status == "unavailable"
    assert payload.availability["sentiment"].reason == "no_evidence"
    # insights：top_posts 仍有证据 → 随缺失字段降级为 partial
    assert payload.availability["insights"].status == "partial"
    assert payload.data_status == "partial"


def test_insights_unavailable_without_sentiment_and_posts() -> None:
    steps = [
        _step("step_1", TOOL_OVERVIEW, start="2026-06-01", end="2026-06-30", goal="current: 当期"),
    ]
    notes = [
        _note("step_1", TOOL_OVERVIEW, _summary(_overview_rows((1000, 2000)))),
    ]
    payload = assemble_brand_report(_plan(steps, notes), PARAMS)
    assert payload.availability["insights"].status == "unavailable"
    assert payload.availability["insights"].reason == "no_evidence"


def test_v2_trajectory_supported() -> None:
    goal_slice = {
        "steps": [
            _step("g1_step_1", TOOL_OVERVIEW, start="2026-06-01", end="2026-06-30",
                  goal="current: 当期概览"),
            _step("g1_step_2", TOOL_OVERVIEW, start=MOM_WINDOW[0], end=MOM_WINDOW[1],
                  goal="mom: 环比概览"),
        ],
        "results": [
            _note("g1_step_1", TOOL_OVERVIEW, _summary([{"平台": "小红书", "声量": 900}])),
            _note("g1_step_2", TOOL_OVERVIEW, _summary([{"平台": "小红书", "声量": 300}])),
        ],
    }
    plan = {"schema": "agent_trajectory_v2", "goals": {"goal-1": goal_slice}}
    payload = assemble_brand_report(plan, _minimal_params())
    totals = payload.data.overview.total_mentions
    assert totals.current == 900
    assert totals.mom.value == 300
    assert {entry.step_id for entry in payload.sources} == {"g1_step_1", "g1_step_2"}


def _v2_dual_goal_plan() -> dict[str, object]:
    """双 goal v2 轨迹：品牌切片 overview+热帖；kol 切片 raw_posts+tag（污染源）。"""
    brand_slice = {
        "steps": [
            _step("g1_step_1", TOOL_OVERVIEW, start="2026-06-01", end="2026-06-30",
                  goal="current: 当期概览"),
            _step("g1_step_2", TOOL_RAW_POSTS, start="2026-06-01", end="2026-06-30",
                  goal="current: 品牌热门原帖"),
        ],
        "results": [
            _note("g1_step_1", TOOL_OVERVIEW, _summary([{"平台": "小红书", "声量": 900}])),
            _note(
                "g1_step_2",
                TOOL_RAW_POSTS,
                _summary([{"平台": "小红书", "帖子ID": "xhs-b1", "标题": "品牌切片热帖", "互动数": 500}]),
            ),
        ],
    }
    kol_slice = {
        "steps": [
            _step("g2_step_1", TOOL_RAW_POSTS, start="2026-06-01", end="2026-06-30",
                  goal="current: kol 原帖"),
            _step("g2_step_2", TOOL_TAG, goal="current: kol 标签"),
        ],
        "results": [
            _note(
                "g2_step_1",
                TOOL_RAW_POSTS,
                _summary([{"平台": "抖音", "作品ID": "dy-k1", "标题": "KOL切片污染帖", "互动数": 9999}]),
            ),
            _note("g2_step_2", TOOL_TAG, _summary([{"标签名称": "海底捞"}])),
        ],
    }
    return {
        "schema": "agent_trajectory_v2",
        "goals": {"goal-brand": brand_slice, "goal-kol": kol_slice},
    }


def test_v2_goal_id_filters_other_goal_slices() -> None:
    """v2 多 goal 轨迹：goal_id 只恢复本 goal 切片，kol 切片证据不得污染品牌章节。"""
    payload = assemble_brand_report(_v2_dual_goal_plan(), _minimal_params(), goal_id="goal-brand")

    assert payload.data.overview.total_mentions.current == 900
    assert [post.title for post in payload.data.top_posts] == ["品牌切片热帖"]
    # kol 切片的标签匹配不得进入 query_spec。
    assert payload.query_spec.matched_tag is None
    assert payload.query_spec.fallback_keyword == "肯德基"
    assert {entry.step_id for entry in payload.sources} == {"g1_step_1", "g1_step_2"}


def test_v2_goal_id_missing_slice_raises_no_evidence() -> None:
    """goal_id 切片缺失：按空证据处理，走 no_evidence_collected 门禁。"""
    with pytest.raises(LookupError, match="no_evidence_collected"):
        assemble_brand_report(_v2_dual_goal_plan(), _minimal_params(), goal_id="goal-nonexistent")


def test_v2_without_goal_id_merges_all_slices() -> None:
    """不传 goal_id 保持旧行为：合并所有切片（向后兼容）。"""
    payload = assemble_brand_report(_v2_dual_goal_plan(), _minimal_params())

    assert payload.query_spec.matched_tag == "海底捞"
    assert {post.title for post in payload.data.top_posts} == {"品牌切片热帖", "KOL切片污染帖"}


def test_overview_skips_aggregate_rows() -> None:
    """合计/全部/总计/all 平台行跳过：与平台明细行并存时防双计。"""
    rows = [
        {"平台": "小红书", "声量": 1000, "互动数": 800},
        {"平台": "合计", "声量": 99999, "互动数": 99999},
        {"平台": "全部", "声量": 88888},
        {"平台": "总计", "声量": 66666},
        {"平台": "all", "声量": 77777},
    ]
    steps = [
        _step("step_1", TOOL_OVERVIEW, start="2026-06-01", end="2026-06-30", goal="current: 当期")
    ]
    notes = [_note("step_1", TOOL_OVERVIEW, _summary(rows))]

    payload = assemble_brand_report(_plan(steps, notes), PARAMS)

    assert payload.data.overview.total_mentions.current == 1000
    assert payload.data.overview.total_interactions.current == 800
    assert [item.platform for item in payload.data.overview.platforms] == ["xiaohongshu"]


def test_overview_aggregate_only_rows_are_used() -> None:
    """上游只返回聚合行（无平台键的合计记录）时，聚合行是唯一数据，必须使用。

    真实案例（2026-07-31 蔚来任务）：多数据源 overview 返回单条无平台键的
    合计行，全部被跳过会误报 no_evidence_collected。
    """
    rows = [
        {
            "声量": 156438,
            "互动数": 12283794,
            "正面声量数": 23817,
            "负面声量数": 6328,
            "中性声量数": 126293,
        }
    ]
    steps = [
        _step("step_1", TOOL_OVERVIEW, start="2026-06-01", end="2026-06-30", goal="current: 当期")
    ]
    notes = [_note("step_1", TOOL_OVERVIEW, _summary(rows))]

    payload = assemble_brand_report(_plan(steps, notes), PARAMS)

    assert payload.data.overview.total_mentions.current == 156438
    assert payload.data.overview.total_interactions.current == 12283794
    assert [item.platform for item in payload.data.overview.platforms] == ["all"]
    split = payload.data.overview.sentiment_split
    assert split.positive == 23817
    assert split.negative == 6328
    assert split.neutral == 126293


def test_truncated_or_unparseable_summary_ignored() -> None:
    # sanitize_evidence 超长截断后 summary 是非法 JSON 字符串：不得崩溃，按无数据处理。
    steps = [
        _step("step_1", TOOL_OVERVIEW, start="2026-06-01", end="2026-06-30", goal="current: 当期"),
        _step("step_2", TOOL_TREND, start="2026-06-01", end="2026-06-30", goal="current: 趋势"),
    ]
    notes = [
        _note("step_1", TOOL_OVERVIEW, _summary([{"平台": "小红书", "声量": 900}])),
        _note("step_2", TOOL_TREND, '{"日期": "2026-06-01", "声量": 12…(truncated)'),
    ]
    payload = assemble_brand_report(_plan(steps, notes), PARAMS)
    assert payload.data.daily_trend.points == []
    assert payload.availability["daily_trend"].status == "unavailable"

