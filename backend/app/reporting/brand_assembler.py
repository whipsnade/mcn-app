"""品牌报告 v2 数据组装器：task.plan_json 的 settled 证据 → brand_report_v2 快照。

职责边界：
- 只消费 ``EvidenceNote.summary``（settled 时为脱敏后的完整 structured_content，
  常见形态是 DataTap 的 ``{"result": "<json string>"}`` 包装；超过长度上限会被
  截断成非法 JSON 字符串，按无数据跳过，不得崩溃）。
- 期别判定：``TrajectoryStep.evidence_goal`` 的 ``current:``/``mom:``/``yoy:``
  前缀优先；缺失时用 ``arguments`` 起止日期与 ``comparison_windows`` 结果精确
  匹配兜底；都判不出按 current 处理。
- 轨迹 v1（agent_trajectory_v1）与 v2（agent_trajectory_v2，按 goal 分片）都
  支持；v2 合并所有 goal 切片的证据——品牌章节只消费 insight 统计/原帖工具，
  其他 goal 的 KOL 工具不会映射进品牌章节。
- 对比期数值与环比/同比百分比全部由本模块按 data 计算，不依赖模型。
- 轨迹 EvidenceNote 不携带采集时间戳：availability/sources 的 collected_at
  恒为 null（源级时间戳需关联 mcp_calls，超出组装器输入）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import math
from typing import Any, Literal, NamedTuple

from pydantic import ValidationError

from app.goals.schemas import GoalPeriod
from app.orchestration.loop import AgentTrajectory, restore_agent_trajectory
from app.reporting.brand_payload import (
    ALL_CHAPTERS,
    DATA_CHAPTERS,
    BrandReportData,
    BrandReportPayload,
    ChapterAvailability,
    ContentTypeRow,
    CreatorTierRow,
    DailyTrendSection,
    MetricComparison,
    OrganicVsPaid,
    OverviewSection,
    PeriodValue,
    PlatformOverview,
    QuerySpec,
    RegionRow,
    ReportScope,
    SentimentRow,
    SentimentSection,
    SentimentSplit,
    SourceEntry,
    TopPostRow,
    TrendPoint,
)


# ---------------------------------------------------------------------------
# 对比窗
# ---------------------------------------------------------------------------


def comparison_windows(
    period: GoalPeriod, mode: Literal["mom", "mom_yoy"]
) -> dict[str, tuple[date, date]]:
    """mom=紧邻上一等长周期；yoy=起止日期各平移一年（2/29→2/28）。

    mode="mom" 时返回 dict 不含 yoy 键；period 起止非法（不可解析或 end<start）
    抛 ``ValueError("invalid_period")``。
    """
    start = _parse_date(period.start)
    end = _parse_date(period.end)
    if start is None or end is None or end < start:
        raise ValueError("invalid_period")
    length = (end - start).days + 1
    windows = {"mom": (start - timedelta(days=length), start - timedelta(days=1))}
    if mode == "mom_yoy":
        windows["yoy"] = (_shift_year(start), _shift_year(end))
    return windows


def _shift_year(value: date) -> date:
    try:
        return value.replace(year=value.year - 1)
    except ValueError:
        # 2 月 29 日向前平移为 2 月 28 日。
        return value.replace(year=value.year - 1, day=28)


# ---------------------------------------------------------------------------
# 工具名 → 章节映射与字段别名（防御式归一，参照 selection/normalizers.py）
# ---------------------------------------------------------------------------

_TOOL_CHAPTER_HINTS: tuple[tuple[str, str], ...] = (
    ("overview", "overview"),
    ("user.profile", "audience"),
    ("user_profile", "audience"),
    ("audience", "audience"),
    ("hot.topic", "topics"),
    ("hot_topic", "topics"),
    ("topic", "topics"),
    ("raw.posts", "posts"),
    ("raw_posts", "posts"),
    ("best.tag", "tag_match"),
    ("best_tag", "tag_match"),
    ("mentions_tag", "tag_match"),
    ("query.analysis", "analysis"),
    ("query_analysis", "analysis"),
    ("trend", "trend"),
)


def _chapter_of(tool: str) -> str | None:
    lowered = tool.casefold()
    for hint, chapter in _TOOL_CHAPTER_HINTS:
        if hint in lowered:
            return chapter
    return None


_PLATFORM_ALIASES = {
    "小红书": "xiaohongshu",
    "xiaohongshu": "xiaohongshu",
    "抖音": "douyin",
    "douyin": "douyin",
    "微博": "weibo",
    "weibo": "weibo",
    "微信": "wechat",
    "wechat": "wechat",
    "b站": "bilibili",
    "哔哩哔哩": "bilibili",
    "bilibili": "bilibili",
}
_PLATFORM_ORDER = ("xiaohongshu", "douyin", "weibo", "wechat", "bilibili")

_PLATFORM_KEYS = ("平台", "platform", "媒体", "媒介", "datasource", "数据源")
_MENTIONS_KEYS = ("声量", "品牌声量", "品牌提及量", "发帖数", "帖子数", "brand_mentions", "volume")
_EXPOSURE_KEYS = ("曝光量", "曝光数", "曝光", "阅读数", "播放数", "播放量", "exposure")
_INTERACTIONS_KEYS = ("互动数", "互动量", "互动", "interactions")
_POSITIVE_KEYS = ("正面声量", "正面", "positive")
_NEUTRAL_KEYS = ("中性声量", "中性", "neutral")
_NEGATIVE_KEYS = ("负面声量", "负面", "negative")
_DATE_KEYS = ("日期", "时间", "date", "published_at")
_SENTIMENT_KEYS = ("情感", "内容情感", "情绪", "sentiment")
_REGION_KEYS = ("地区", "省份", "地域", "region", "province")
_REGION_MAP_KEYS = ("地域分布", "省份分布", "地区分布")
_CONTENT_TYPE_KEYS = ("内容类型", "内容形式", "类型", "content_type")
_TIER_KEYS = ("达人层级", "创作者层级", "粉丝层级", "creator_tier", "tier")
_COMMERCIAL_KEYS = ("是否商单", "商业属性", "内容属性", "是否广告", "is_commercial")
_TAG_KEYS = ("标签名称", "标签名", "品牌标签", "品类标签", "matched_tag", "tag", "标签")
_POST_ID_KEYS = ("帖子ID", "帖子id", "笔记ID", "视频ID", "作品ID", "post_id", "aweme_id", "id")
_TITLE_KEYS = ("标题", "笔记标题", "内容", "title")
_AUTHOR_KEYS = ("昵称", "作者", "用户昵称", "达人昵称", "author")
_POST_DATE_KEYS = ("发布时间", "采集时间", "publish_time", "collected_at")
_LIKE_KEYS = ("点赞数", "点赞", "likes", "like_count")
_COMMENT_KEYS = ("评论数", "评论", "comments", "comment_count")
_COLLECT_KEYS = ("收藏数", "收藏", "collects", "collect_count")
_SHARE_KEYS = ("分享数", "转发数", "转发", "分享", "shares", "share_count")
_FOLLOWER_KEYS = ("粉丝数", "用户粉丝数", "粉丝数量", "followers")
_URL_KEYS = ("帖子链接", "原帖链接", "链接", "url")
_RELEVANCE_KEYS = ("品牌相关", "是否相关", "is_brand_related")


class _Evidence(NamedTuple):
    tool: str
    step_id: str
    chapter: str | None
    kind: str  # current / mom / yoy
    summary: Any


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def assemble_brand_report(
    task_plan_json: dict[str, Any] | None,
    goal_params: dict[str, Any],
    *,
    warning_code: str | None = None,
) -> BrandReportPayload:
    """settled 证据 → brand_report_v2（data+availability+query_spec+sources；narrative 留空）。

    综合概览最小证据（任一平台当期 overview 指标行）缺失时抛
    ``LookupError("no_evidence_collected")``（由 finalize 映射为构建失败）。
    """
    params = dict(goal_params) if isinstance(goal_params, dict) else {}
    brand = str(params.get("brand") or "").strip()
    mode_raw = params.get("comparison_mode")
    mode: Literal["mom", "mom_yoy"] = "mom_yoy" if mode_raw == "mom_yoy" else "mom"
    platforms = [str(item).strip() for item in params.get("platforms") or [] if str(item).strip()]

    current_window = _period_window(params.get("period"))
    windows: dict[str, tuple[date, date]] = {}
    if current_window is not None:
        try:
            windows = comparison_windows(
                GoalPeriod(start=current_window[0].isoformat(), end=current_window[1].isoformat()),
                mode,
            )
        except ValueError:
            current_window = None
            windows = {}

    settled, failed = _bucket_evidence(task_plan_json, current_window, windows)

    # ---- 综合概览（最小证据门禁） ----
    overview_ev = [e for e in settled if e.chapter == "overview"]
    current_overview, split = _aggregate_overview([e for e in overview_ev if e.kind == "current"])
    if not current_overview:
        raise LookupError("no_evidence_collected")
    mom_overview, _ = _aggregate_overview([e for e in overview_ev if e.kind == "mom"])
    yoy_overview, _ = _aggregate_overview([e for e in overview_ev if e.kind == "yoy"])
    overview, overview_missing = _build_overview_section(
        current_overview,
        split,
        mom_overview,
        yoy_overview,
        mode=mode,
        period_valid=current_window is not None,
        has_mom_evidence=any(e.kind == "mom" for e in overview_ev),
        has_yoy_evidence=any(e.kind == "yoy" for e in overview_ev),
        mom_failed=any(e.chapter == "overview" and e.kind == "mom" for e in failed),
        yoy_failed=any(e.chapter == "overview" and e.kind == "yoy" for e in failed),
    )

    # ---- 其余章节 ----
    trend_ev = [e for e in settled if e.chapter == "trend" and e.kind == "current"]
    daily_trend, trend_contrib, max_trend_date = _build_trend(trend_ev)

    sentiment_rows, sentiment_contrib = _build_sentiment(
        [e for e in settled if e.chapter in ("analysis", "overview") and e.kind == "current"]
    )
    dimension_ev = [
        e for e in settled if e.chapter in ("analysis", "topics") and e.kind == "current"
    ]
    content_types, content_contrib = _build_share_rows(
        dimension_ev, _CONTENT_TYPE_KEYS, ContentTypeRow, "content_type"
    )
    creator_tiers, tier_contrib = _build_share_rows(
        dimension_ev, _TIER_KEYS, CreatorTierRow, "tier"
    )
    organic_vs_paid, organic_contrib = _build_organic(dimension_ev)
    regions, region_contrib = _build_regions(
        [e for e in settled if e.chapter in ("audience", "analysis") and e.kind == "current"]
    )
    top_posts, post_contrib = _build_top_posts(
        [e for e in settled if e.chapter == "posts" and e.kind == "current"]
    )

    # ---- scope ----
    data_as_of: str | None = None
    if current_window is not None and max_trend_date is not None:
        if max_trend_date < current_window[1]:
            data_as_of = max_trend_date.isoformat()
    scope = ReportScope(
        brand=brand,
        period_start=current_window[0].isoformat() if current_window else None,
        period_end=current_window[1].isoformat() if current_window else None,
        platforms=platforms,
        comparison_mode=mode,
        data_as_of=data_as_of,
    )
    query_spec = QuerySpec(
        original_term=brand,
        matched_tag=_matched_tag([e for e in settled if e.chapter == "tag_match"]),
        fallback_keyword=None,
        comparison_definition=_comparison_definition(current_window, windows),
    )
    if query_spec.matched_tag is None:
        query_spec.fallback_keyword = brand or None

    # ---- availability ----
    availability = _build_availability(
        overview_missing=overview_missing,
        overview_tools=[e.tool for e in overview_ev if e.kind == "current"],
        sentiment_rows=sentiment_rows,
        sentiment_tools=[e.tool for e in sentiment_contrib],
        sentiment_failed=any(e.chapter == "analysis" for e in failed),
        daily_trend=daily_trend,
        trend_tools=[e.tool for e in trend_contrib],
        trend_failed=any(e.chapter == "trend" for e in failed),
        data_as_of=data_as_of,
        content_types=content_types,
        creator_tiers=creator_tiers,
        organic_vs_paid=organic_vs_paid,
        dimension_tools=[
            entry.tool
            for entry in _dedupe_evidence([*content_contrib, *tier_contrib, *organic_contrib])
        ],
        regions=regions,
        region_tools=[e.tool for e in region_contrib],
        top_posts=top_posts,
        post_tools=[e.tool for e in post_contrib],
        requested_platforms=[_canon_platform(p) for p in platforms],
        all_tools=[e.tool for e in settled],
    )
    _merge_warning(availability, warning_code)

    data_status: Literal["complete", "partial"] = (
        "complete"
        if all(availability[key].status == "complete" for key in DATA_CHAPTERS)
        else "partial"
    )
    data = BrandReportData(
        overview=overview,
        sentiment=SentimentSection(rows=sentiment_rows),
        daily_trend=daily_trend,
        content_types=content_types,
        creator_tiers=creator_tiers,
        organic_vs_paid=organic_vs_paid,
        regions=regions,
        top_posts=top_posts,
    )
    return BrandReportPayload(
        data_status=data_status,
        scope=scope,
        query_spec=query_spec,
        data=data,
        availability=availability,
        sources=[
            SourceEntry(tool=e.tool, step_id=e.step_id, collected_at=None) for e in settled
        ],
    )


# ---------------------------------------------------------------------------
# 轨迹恢复与证据分桶
# ---------------------------------------------------------------------------


def _restore_trajectory(plan_json: dict[str, Any] | None) -> AgentTrajectory:
    """v1 直接恢复；v2 合并所有 goal 切片（step id 已按 g{seq}_ 命名空间隔离）。"""
    if isinstance(plan_json, dict) and plan_json.get("schema") == "agent_trajectory_v2":
        merged = AgentTrajectory()
        goals = plan_json.get("goals")
        if isinstance(goals, dict):
            for goal_slice in goals.values():
                try:
                    slice_trajectory = AgentTrajectory.model_validate(goal_slice)
                except ValidationError:
                    continue
                merged.steps.extend(slice_trajectory.steps)
                merged.results.extend(slice_trajectory.results)
        return merged
    return restore_agent_trajectory(plan_json)


def _bucket_evidence(
    plan_json: dict[str, Any] | None,
    current_window: tuple[date, date] | None,
    windows: dict[str, tuple[date, date]],
) -> tuple[list[_Evidence], list[_Evidence]]:
    trajectory = _restore_trajectory(plan_json)
    steps_by_id = {step.id: step for step in trajectory.steps}
    settled: list[_Evidence] = []
    failed: list[_Evidence] = []
    for note in trajectory.results:
        step = steps_by_id.get(note.step_id)
        kind = _period_kind(step, current_window, windows)
        entry = _Evidence(
            tool=note.tool,
            step_id=note.step_id,
            chapter=_chapter_of(note.tool),
            kind=kind,
            summary=note.summary,
        )
        (settled if note.status == "settled" else failed).append(entry)
    return settled, failed


def _dedupe_evidence(evidences: list[_Evidence]) -> list[_Evidence]:
    """按 step_id 去重保序（同一调用可能向多个维度贡献行）。"""
    seen: set[str] = set()
    result: list[_Evidence] = []
    for evidence in evidences:
        if evidence.step_id in seen:
            continue
        seen.add(evidence.step_id)
        result.append(evidence)
    return result


def _period_kind(
    step: Any,
    current_window: tuple[date, date] | None,
    windows: dict[str, tuple[date, date]],
) -> str:
    """evidence_goal 期别前缀优先；arguments 日期精确匹配兜底；判不出按 current。"""
    if step is not None:
        goal_text = (getattr(step, "evidence_goal", "") or "").strip().casefold()
        for prefix in ("current", "mom", "yoy"):
            if goal_text.startswith(f"{prefix}:") or goal_text.startswith(f"{prefix}："):
                return prefix
        arguments = getattr(step, "arguments", None)
        if isinstance(arguments, dict):
            start = _parse_date(arguments.get("start_time"))
            end = _parse_date(arguments.get("end_time"))
            if start is not None and end is not None:
                if current_window is not None and (start, end) == current_window:
                    return "current"
                for name, window in windows.items():
                    if (start, end) == window:
                        return name
    return "current"


# ---------------------------------------------------------------------------
# 章节数据归一
# ---------------------------------------------------------------------------


def _aggregate_overview(
    evidences: list[_Evidence],
) -> tuple[dict[str, dict[str, float | None]], dict[str, float | None]]:
    """按平台合并 overview 行；同平台多行数值累加，缺失保持 None。"""
    per_platform: dict[str, dict[str, float | None]] = {}
    split: dict[str, float | None] = {"positive": None, "neutral": None, "negative": None}
    metric_aliases = (
        ("mentions", _MENTIONS_KEYS),
        ("exposure", _EXPOSURE_KEYS),
        ("interactions", _INTERACTIONS_KEYS),
    )
    split_aliases = (
        ("positive", _POSITIVE_KEYS),
        ("neutral", _NEUTRAL_KEYS),
        ("negative", _NEGATIVE_KEYS),
    )
    for evidence in evidences:
        for raw in _iter_rows(evidence.summary):
            if not _has_any(raw, _MENTIONS_KEYS + _EXPOSURE_KEYS + _INTERACTIONS_KEYS):
                continue
            platform = _canon_platform(_first(raw, _PLATFORM_KEYS))
            slot = per_platform.setdefault(
                platform, {"mentions": None, "exposure": None, "interactions": None}
            )
            for metric, aliases in metric_aliases:
                value = _num(_first(raw, aliases))
                if value is not None:
                    slot[metric] = (slot[metric] or 0.0) + value
            for name, aliases in split_aliases:
                value = _num(_first(raw, aliases))
                if value is not None:
                    split[name] = (split[name] or 0.0) + value
    return per_platform, split


def _totals(per_platform: dict[str, dict[str, float | None]]) -> dict[str, float | None]:
    totals: dict[str, float | None] = {}
    for metric in ("mentions", "exposure", "interactions"):
        values = [row[metric] for row in per_platform.values() if row[metric] is not None]
        totals[metric] = sum(values) if values else None
    return totals


def _build_overview_section(
    current: dict[str, dict[str, float | None]],
    split: dict[str, float | None],
    mom: dict[str, dict[str, float | None]],
    yoy: dict[str, dict[str, float | None]],
    *,
    mode: Literal["mom", "mom_yoy"],
    period_valid: bool,
    has_mom_evidence: bool,
    has_yoy_evidence: bool,
    mom_failed: bool,
    yoy_failed: bool,
) -> tuple[OverviewSection, list[str]]:
    current_totals = _totals(current)
    mom_totals = _totals(mom) if has_mom_evidence else None
    yoy_totals = _totals(yoy) if has_yoy_evidence else None
    mom_state = _prev_state(
        requested=True,
        period_valid=period_valid,
        has_evidence=has_mom_evidence,
        has_failed=mom_failed,
    )
    yoy_state = _prev_state(
        requested=mode == "mom_yoy",
        period_valid=period_valid,
        has_evidence=has_yoy_evidence,
        has_failed=yoy_failed,
    )
    comparisons: dict[str, MetricComparison] = {}
    for metric in ("mentions", "exposure", "interactions"):
        comparisons[metric] = _metric_comparison(
            current_totals[metric],
            mom_totals[metric] if mom_totals else None,
            mom_state,
            yoy_totals[metric] if yoy_totals else None,
            yoy_state,
        )
    platforms = [
        PlatformOverview(
            platform=platform,
            mentions=values["mentions"],
            exposure=values["exposure"],
            interactions=values["interactions"],
        )
        for platform, values in sorted(current.items(), key=_platform_sort_key)
    ]
    missing = [
        metric for metric in ("mentions", "exposure", "interactions") if current_totals[metric] is None
    ]
    if all(split[name] is None for name in ("positive", "neutral", "negative")):
        missing.append("sentiment_split")
    section = OverviewSection(
        platforms=platforms,
        total_mentions=comparisons["mentions"],
        total_exposure=comparisons["exposure"],
        total_interactions=comparisons["interactions"],
        sentiment_split=SentimentSplit(
            positive=split["positive"], neutral=split["neutral"], negative=split["negative"]
        ),
    )
    return section, missing


_PeriodStatus = Literal["ok", "not_requested", "restricted"]


def _prev_state(
    *, requested: bool, period_valid: bool, has_evidence: bool, has_failed: bool
) -> tuple[_PeriodStatus, str | None]:
    if not requested:
        return ("not_requested", None)
    if not period_valid:
        return ("restricted", "invalid_period")
    if has_evidence:
        return ("ok", None)
    if has_failed:
        return ("restricted", "tool_failed")
    return ("restricted", "no_data")


def _metric_comparison(
    current: float | None,
    mom_value: float | None,
    mom_state: tuple[_PeriodStatus, str | None],
    yoy_value: float | None,
    yoy_state: tuple[_PeriodStatus, str | None],
) -> MetricComparison:
    mom_status, mom_reason = mom_state
    yoy_status, yoy_reason = yoy_state
    return MetricComparison(
        current=current,
        mom=PeriodValue(
            value=mom_value if mom_status == "ok" else None,
            status=mom_status,
            reason=mom_reason,
        ),
        yoy=PeriodValue(
            value=yoy_value if yoy_status == "ok" else None,
            status=yoy_status,
            reason=yoy_reason,
        ),
        mom_change_pct=_change_pct(current, mom_value if mom_status == "ok" else None),
        yoy_change_pct=_change_pct(current, yoy_value if yoy_status == "ok" else None),
    )


def _change_pct(current: float | None, previous: float | None) -> float | None:
    if current is None or previous in (None, 0):
        return None
    return round((current - previous) / previous * 100, 2)


def _build_trend(
    evidences: list[_Evidence],
) -> tuple[DailyTrendSection, list[_Evidence], date | None]:
    by_date: dict[date, dict[str, float | None]] = {}
    contributing: list[_Evidence] = []
    for evidence in evidences:
        rows = list(_iter_rows(evidence.summary))
        used = False
        for raw in rows:
            day = _parse_date(_first(raw, _DATE_KEYS))
            if day is None:
                continue
            mentions = _num(_first(raw, _MENTIONS_KEYS))
            interactions = _num(_first(raw, _INTERACTIONS_KEYS))
            if mentions is None and interactions is None:
                continue
            slot = by_date.setdefault(day, {"mentions": None, "interactions": None})
            if mentions is not None:
                slot["mentions"] = (slot["mentions"] or 0.0) + mentions
            if interactions is not None:
                slot["interactions"] = (slot["interactions"] or 0.0) + interactions
            used = True
        if used:
            contributing.append(evidence)
    points = [
        TrendPoint(date=day.isoformat(), mentions=values["mentions"], interactions=values["interactions"])
        for day, values in sorted(by_date.items())
    ]
    peak_date: str | None = None
    peak_mentions: float | None = None
    mention_points = [point for point in points if point.mentions is not None]
    if mention_points:
        peak = max(mention_points, key=lambda point: point.mentions or 0.0)
        peak_date, peak_mentions = peak.date, peak.mentions
    max_date = max(by_date) if by_date else None
    return (
        DailyTrendSection(points=points, peak_date=peak_date, peak_mentions=peak_mentions),
        contributing,
        max_date,
    )


def _build_sentiment(evidences: list[_Evidence]) -> tuple[list[SentimentRow], list[_Evidence]]:
    rows: list[SentimentRow] = []
    contributing: list[_Evidence] = []
    for evidence in evidences:
        used = False
        for raw in _iter_rows(evidence.summary):
            sentiment = _text(_first(raw, _SENTIMENT_KEYS))
            if sentiment is None:
                continue
            rows.append(
                SentimentRow(
                    platform=_canon_platform(_first(raw, _PLATFORM_KEYS)),
                    sentiment=sentiment,
                    mentions=_num(_first(raw, _MENTIONS_KEYS)),
                    interactions=_num(_first(raw, _INTERACTIONS_KEYS)),
                )
            )
            used = True
        if used:
            contributing.append(evidence)
    platform_totals: dict[str, float] = {}
    for row in rows:
        if row.mentions is not None:
            platform_totals[row.platform] = platform_totals.get(row.platform, 0.0) + row.mentions
    for row in rows:
        total = platform_totals.get(row.platform)
        if row.mentions is not None and total:
            row.share_pct = round(row.mentions / total * 100, 2)
    return rows, contributing


def _build_share_rows(
    evidences: list[_Evidence],
    label_keys: tuple[str, ...],
    model: type[ContentTypeRow] | type[CreatorTierRow],
    label_field: str,
) -> tuple[list[Any], list[_Evidence]]:
    """内容类型/达人层级共用的「标签 + 声量 + 占比」归一。"""
    merged: dict[str, float | None] = {}
    contributing: list[_Evidence] = []
    for evidence in evidences:
        used = False
        for raw in _iter_rows(evidence.summary):
            label = _text(_first(raw, label_keys))
            if label is None:
                continue
            mentions = _num(_first(raw, _MENTIONS_KEYS))
            if label in merged:
                if mentions is not None:
                    merged[label] = (merged[label] or 0.0) + mentions
            else:
                merged[label] = mentions
            used = True
        if used:
            contributing.append(evidence)
    total = sum(value for value in merged.values() if value is not None)
    rows = []
    for label, mentions in merged.items():
        share = round(mentions / total * 100, 2) if mentions is not None and total else None
        rows.append(model(**{label_field: label, "mentions": mentions, "share_pct": share}))
    rows.sort(key=lambda row: (row.mentions is None, -(row.mentions or 0.0)))
    return rows, contributing


_PAID_TERMS = ("商单", "广告", "是", "commercial", "paid", "true")
_ORGANIC_TERMS = ("自然", "否", "非商单", "organic", "false")


def _build_organic(evidences: list[_Evidence]) -> tuple[OrganicVsPaid, list[_Evidence]]:
    organic: float | None = None
    paid: float | None = None
    contributing: list[_Evidence] = []
    for evidence in evidences:
        used = False
        for raw in _iter_rows(evidence.summary):
            flag = _first(raw, _COMMERCIAL_KEYS)
            mentions = _num(_first(raw, _MENTIONS_KEYS))
            if flag is None or mentions is None:
                continue
            text = str(flag).strip().casefold()
            if any(term in text for term in _ORGANIC_TERMS):
                organic = (organic or 0.0) + mentions
                used = True
            elif any(term in text for term in _PAID_TERMS):
                paid = (paid or 0.0) + mentions
                used = True
        if used:
            contributing.append(evidence)
    total = (organic or 0.0) + (paid or 0.0)
    return (
        OrganicVsPaid(
            organic_mentions=organic,
            paid_mentions=paid,
            organic_share_pct=round(organic / total * 100, 2) if organic is not None and total else None,
            paid_share_pct=round(paid / total * 100, 2) if paid is not None and total else None,
        ),
        contributing,
    )


def _build_regions(evidences: list[_Evidence]) -> tuple[list[RegionRow], list[_Evidence]]:
    merged: dict[str, dict[str, float | None]] = {}
    contributing: list[_Evidence] = []
    for evidence in evidences:
        used = False
        for raw in _iter_rows(evidence.summary):
            # 映射形态：{"地域分布": {"广东": 123, ...}}
            for map_key in _REGION_MAP_KEYS:
                mapping = raw.get(map_key)
                if isinstance(mapping, dict):
                    for region, value in mapping.items():
                        mentions = _num(value)
                        if mentions is None:
                            continue
                        slot = merged.setdefault(str(region), {"mentions": None, "interactions": None})
                        slot["mentions"] = (slot["mentions"] or 0.0) + mentions
                        used = True
            region = _text(_first(raw, _REGION_KEYS))
            if region is None:
                continue
            mentions = _num(_first(raw, _MENTIONS_KEYS))
            interactions = _num(_first(raw, _INTERACTIONS_KEYS))
            if mentions is None and interactions is None:
                continue
            slot = merged.setdefault(region, {"mentions": None, "interactions": None})
            if mentions is not None:
                slot["mentions"] = (slot["mentions"] or 0.0) + mentions
            if interactions is not None:
                slot["interactions"] = (slot["interactions"] or 0.0) + interactions
            used = True
        if used:
            contributing.append(evidence)
    total = sum(
        values["mentions"] for values in merged.values() if values["mentions"] is not None
    )
    rows = [
        RegionRow(
            region=region,
            mentions=values["mentions"],
            interactions=values["interactions"],
            share_pct=(
                round(values["mentions"] / total * 100, 2)
                if values["mentions"] is not None and total
                else None
            ),
        )
        for region, values in merged.items()
    ]
    rows.sort(key=lambda row: (row.mentions is None, -(row.mentions or 0.0)))
    return rows[:20], contributing


def _build_top_posts(evidences: list[_Evidence]) -> tuple[list[TopPostRow], list[_Evidence]]:
    rows: list[TopPostRow] = []
    contributing: list[_Evidence] = []
    for evidence in evidences:
        used = False
        for raw in _iter_rows(evidence.summary):
            if not _has_any(raw, _POST_ID_KEYS + _TITLE_KEYS + _URL_KEYS):
                continue
            flag = _first(raw, _RELEVANCE_KEYS)
            if flag is not None and str(flag).strip().casefold() in ("否", "no", "false"):
                continue  # 显式标注非品牌相关：剔除
            rows.append(_post_row(raw))
            used = True
        if used:
            contributing.append(evidence)
    by_platform: dict[str, list[TopPostRow]] = {}
    for row in rows:
        by_platform.setdefault(row.platform, []).append(row)
    ordered: list[TopPostRow] = []
    for platform in sorted(by_platform, key=_platform_sort_key):
        group = sorted(
            by_platform[platform],
            key=lambda row: (row.interactions is None, -(row.interactions or 0)),
        )
        ordered.extend(group[:15])
    return ordered, contributing


def _post_row(raw: dict[str, Any]) -> TopPostRow:
    followers = _num(_first(raw, _FOLLOWER_KEYS))
    tier = _text(_first(raw, _TIER_KEYS)) or _tier_from_followers(followers)
    collected = _parse_date(_first(raw, _POST_DATE_KEYS))
    return TopPostRow(
        platform=_canon_platform(_first(raw, _PLATFORM_KEYS)),
        post_id=_text(_first(raw, _POST_ID_KEYS)),
        collected_at=collected.isoformat() if collected else None,
        title=_text(_first(raw, _TITLE_KEYS)),
        author=_text(_first(raw, _AUTHOR_KEYS)),
        interactions=_int(_first(raw, _INTERACTIONS_KEYS)),
        exposure_count=_int(_first(raw, _EXPOSURE_KEYS)),
        like_count=_int(_first(raw, _LIKE_KEYS)),
        comment_count=_int(_first(raw, _COMMENT_KEYS)),
        collect_count=_int(_first(raw, _COLLECT_KEYS)),
        share_count=_int(_first(raw, _SHARE_KEYS)),
        sentiment=_text(_first(raw, _SENTIMENT_KEYS)),
        creator_tier=tier,
        url=_valid_url(_first(raw, _URL_KEYS)),
    )


def _tier_from_followers(followers: float | None) -> str | None:
    if followers is None:
        return None
    if followers >= 1_000_000:
        return "头部达人"
    if followers >= 100_000:
        return "腰部达人"
    if followers >= 10_000:
        return "尾部达人"
    return "素人"


def _matched_tag(evidences: list[_Evidence]) -> str | None:
    for evidence in evidences:
        for raw in _iter_rows(evidence.summary):
            tag = _text(_first(raw, _TAG_KEYS))
            if tag is not None:
                return tag
    return None


def _comparison_definition(
    current_window: tuple[date, date] | None,
    windows: dict[str, tuple[date, date]],
) -> str:
    if current_window is None:
        return "无有效分析周期，未进行对比查询"
    parts = [f"当期 {current_window[0].isoformat()}~{current_window[1].isoformat()}"]
    if "mom" in windows:
        parts.append(f"环比 {windows['mom'][0].isoformat()}~{windows['mom'][1].isoformat()}")
    if "yoy" in windows:
        parts.append(f"同比 {windows['yoy'][0].isoformat()}~{windows['yoy'][1].isoformat()}")
    return "；".join(parts)


# ---------------------------------------------------------------------------
# availability 聚合
# ---------------------------------------------------------------------------


def _chapter(
    status: Literal["complete", "partial", "unavailable"],
    *,
    missing: list[str] | None = None,
    reason: str | None = None,
    tools: list[str] | None = None,
) -> ChapterAvailability:
    return ChapterAvailability(
        status=status,
        missing_fields=missing or [],
        reason=reason,
        source_tools=sorted(set(tools or [])),
        collected_at=None,
    )


def _unavailable_reason(has_failed: bool) -> str:
    return "tool_failed" if has_failed else "no_evidence"


def _build_availability(
    *,
    overview_missing: list[str],
    overview_tools: list[str],
    sentiment_rows: list[SentimentRow],
    sentiment_tools: list[str],
    sentiment_failed: bool,
    daily_trend: DailyTrendSection,
    trend_tools: list[str],
    trend_failed: bool,
    data_as_of: str | None,
    content_types: list[ContentTypeRow],
    creator_tiers: list[CreatorTierRow],
    organic_vs_paid: OrganicVsPaid,
    dimension_tools: list[str],
    regions: list[RegionRow],
    region_tools: list[str],
    top_posts: list[TopPostRow],
    post_tools: list[str],
    requested_platforms: list[str],
    all_tools: list[str],
) -> dict[str, ChapterAvailability]:
    availability: dict[str, ChapterAvailability] = {}

    availability["overview"] = _chapter(
        "complete" if not overview_missing else "partial",
        missing=overview_missing,
        tools=overview_tools,
    )

    if not sentiment_rows:
        availability["sentiment"] = _chapter(
            "unavailable", reason=_unavailable_reason(sentiment_failed)
        )
    else:
        missing: list[str] = []
        if any(row.mentions is None for row in sentiment_rows):
            missing.append("mentions")
        if all(row.interactions is None for row in sentiment_rows):
            missing.append("interactions")
        availability["sentiment"] = _chapter(
            "complete" if not missing else "partial", missing=missing, tools=sentiment_tools
        )

    if not daily_trend.points:
        availability["daily_trend"] = _chapter(
            "unavailable", reason=_unavailable_reason(trend_failed)
        )
    else:
        trend_missing: list[str] = []
        if all(point.interactions is None for point in daily_trend.points):
            trend_missing.append("interactions")
        if data_as_of is not None:
            trend_missing.append("tail_days")
        availability["daily_trend"] = _chapter(
            "complete" if not trend_missing else "partial",
            missing=trend_missing,
            tools=trend_tools,
        )

    dim_missing: list[str] = []
    if not content_types:
        dim_missing.append("content_types")
    if not creator_tiers:
        dim_missing.append("creator_tiers")
    if organic_vs_paid.organic_mentions is None and organic_vs_paid.paid_mentions is None:
        dim_missing.append("organic_vs_paid")
    if len(dim_missing) == 3:
        availability["content_creators"] = _chapter("unavailable", reason="no_evidence")
    else:
        availability["content_creators"] = _chapter(
            "complete" if not dim_missing else "partial",
            missing=dim_missing,
            tools=dimension_tools,
        )

    if not regions:
        availability["regions"] = _chapter("unavailable", reason="no_evidence")
    else:
        region_missing = (
            ["interactions"] if all(row.interactions is None for row in regions) else []
        )
        availability["regions"] = _chapter(
            "complete" if not region_missing else "partial",
            missing=region_missing,
            tools=region_tools,
        )

    if not top_posts:
        availability["top_posts"] = _chapter("unavailable", reason="no_evidence")
    else:
        post_missing: list[str] = []
        for platform in requested_platforms:
            if platform != "all" and all(row.platform != platform for row in top_posts):
                post_missing.append(f"platform:{platform}")
        for column in ("title", "interactions", "exposure_count", "url"):
            if all(getattr(row, column) is None for row in top_posts):
                post_missing.append(column)
        availability["top_posts"] = _chapter(
            "complete" if not post_missing else "partial",
            missing=post_missing,
            tools=post_tools,
        )

    sentiment_status = availability["sentiment"].status
    posts_status = availability["top_posts"].status
    if sentiment_status == "unavailable" and posts_status == "unavailable":
        availability["insights"] = _chapter("unavailable", reason="no_evidence")
    elif sentiment_status == "complete" and posts_status == "complete":
        availability["insights"] = _chapter(
            "complete",
            tools=availability["sentiment"].source_tools + availability["top_posts"].source_tools,
        )
    else:
        availability["insights"] = _chapter(
            "partial",
            tools=availability["sentiment"].source_tools + availability["top_posts"].source_tools,
        )

    availability["methodology"] = _chapter("complete", tools=all_tools)
    return availability


_WARNING_CHAPTER = {"brand_trend_data_unavailable": "daily_trend"}
# warning code → 人话说明（进叙事 prompt 与导出受限声明）；未知 code 保留原文。
_WARNING_REASON_TEXT = {"brand_trend_data_unavailable": "趋势数据未成功获取"}


def _merge_warning(
    availability: dict[str, ChapterAvailability], warning_code: str | None
) -> None:
    """warning_code（如 brand_trend_data_unavailable）合并进对应章节 reason（人话）。"""
    if not warning_code:
        return
    chapter_key = _WARNING_CHAPTER.get(warning_code)
    if chapter_key is None:
        return
    chapter = availability[chapter_key]
    if chapter.status == "complete":
        return  # 证据完整时以证据为准，warning 不降级
    text = _WARNING_REASON_TEXT.get(warning_code, warning_code)
    chapter.reason = f"{chapter.reason}；{text}" if chapter.reason else text


# ---------------------------------------------------------------------------
# 基础解析工具
# ---------------------------------------------------------------------------


def _parse_summary(summary: Any) -> Any | None:
    """summary → 可遍历 JSON 值；截断串/非法输入返回 None。"""
    value = summary
    if isinstance(value, dict):
        raw = value.get("result")
        if raw is None:
            return value
        value = raw
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _iter_rows(summary: Any, _cap: int = 2000) -> tuple[dict[str, Any], ...]:
    """递归收集 summary 树中的全部 dict（上限防御）。"""
    parsed = _parse_summary(summary)
    dicts: list[dict[str, Any]] = []

    def visit(node: Any) -> None:
        if len(dicts) >= _cap:
            return
        if isinstance(node, dict):
            dicts.append(node)
            for child in node.values():
                if isinstance(child, (dict, list)):
                    visit(child)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(parsed)
    return tuple(dicts)


def _first(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in row and row[key] is not None and row[key] != "":
            return row[key]
    return None


def _has_any(row: dict[str, Any], keys: tuple[str, ...]) -> bool:
    return _first(row, keys) is not None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _num(value: Any) -> float | None:
    """非负有限数值解析；支持 万/亿 中文单位与千分位字符串。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip().replace(",", "").replace(" ", "")
        multiplier = 1.0
        if text.endswith("万"):
            multiplier, text = 1e4, text[:-1]
        elif text.endswith("亿"):
            multiplier, text = 1e8, text[:-1]
        if not text:
            return None
        try:
            number = float(text) * multiplier
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _int(value: Any) -> int | None:
    number = _num(value)
    return int(number) if number is not None else None


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = (
        value.strip().replace("/", "-").replace("年", "-").replace("月", "-").replace("日", "")
    )
    text = text[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _period_window(value: Any) -> tuple[date, date] | None:
    if not isinstance(value, dict):
        return None
    start = _parse_date(value.get("start"))
    end = _parse_date(value.get("end"))
    if start is None or end is None or end < start:
        return None
    return (start, end)


def _canon_platform(value: Any) -> str:
    text = str(value or "").strip().casefold()
    if not text:
        return "all"
    for alias, platform in _PLATFORM_ALIASES.items():
        if alias in text:
            return platform
    return text


def _platform_sort_key(platform: Any) -> tuple[int, str]:
    name = platform[0] if isinstance(platform, tuple) else platform
    try:
        return (_PLATFORM_ORDER.index(str(name)), str(name))
    except ValueError:
        return (len(_PLATFORM_ORDER), str(name))


def _valid_url(value: Any) -> str | None:
    """仅接受 http(s) 绝对 URL；其余（含拼接嫌疑的相对串）一律 None。"""
    text = _text(value)
    if text is None:
        return None
    if text.startswith("http://") or text.startswith("https://"):
        return text
    return None


__all__ = ["ALL_CHAPTERS", "assemble_brand_report", "comparison_windows"]
