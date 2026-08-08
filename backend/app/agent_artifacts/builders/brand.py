"""``brand_report_v3`` Draft builder（设计 §12.1 / v3 加固 §3.3/§6.1，B2）。

模型提供用户确认的 scope、按章节分组的 Evidence（``(evidence_id, raw_payload)``）
与叙事字段；builder 负责确定性聚合、字段级 lineage 与强类型 payload 构造。
builder 不选择 MCP 工具、不发起外部查询、不改变用户目标（§3.3 红线）。

聚合口径移植自已删除的旧 ``reporting/brand_assembler.py``（git ``d54bc06^``）的
确定性部分：

- overview：按平台合并声量/互动/发帖；合计行（全部/合计/总计/all）在存在具名
  平台行时跳过防双计，上游只返回聚合行时归入 ``all`` 平台；``share_of_voice``
  为平台声量/总声量（4 位）；``sentiment_score`` 为净情感指数
  ``(正面 - 负面) / 总量 * 100``（2 位）；
- comparisons：mom = 紧邻上一等长周期，yoy = 起止各平移一年（2/29→2/28）；
  未请求的对比固定 ``not_requested`` 且无 metrics；请求的对比由模型提供
  对应期别的 overview Evidence（``overview_mom`` / ``overview_yoy`` 分组），
  delta/rate 全部由代码计算（rate 保留 4 位）；
- sentiment：行情感计数（声量值、缺失按 1），share 按总量归一；无情感明细
  Evidence 时回退 overview 汇总的正/中/负构成（partial 披露）；
- daily_trend/topics/top_posts：见 sections.py / raw_rows.py。

缺失数值保持 null 并按要求 partial/unavailable + limitation（B1 递归 null
治理），绝不把缺失当 0。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from pydantic import ValidationError

from app.agent_artifacts.builders.common import (
    DraftBuildError,
    DraftBuildResult,
    LineageCollector,
    methodology_dict,
)
from app.agent_artifacts.builders.raw_rows import (
    AGGREGATE_PLATFORM_NAMES,
    COMMERCIAL_KEYS,
    CONTENT_TYPE_KEYS,
    ENGAGEMENT_KEYS,
    NEGATIVE_KEYS,
    NEUTRAL_KEYS,
    PLATFORM_KEYS,
    POSITIVE_KEYS,
    POSTS_KEYS,
    REGION_KEYS,
    REGION_MAP_KEYS,
    SENTIMENT_KEYS,
    TIER_KEYS,
    TIME_KEYS,
    TOPIC_KEYS,
    VOLUME_KEYS,
    RowRef,
    canon_platform,
    canonicalize_marketing_evidence,
    commercial_kind,
    first,
    has_any,
    num,
    parse_date,
    platform_coverage_incomplete,
    platform_sort_key,
    reject_exclusive_group_evidence_reuse,
    sentiment_score,
    text,
)
from app.agent_artifacts.builders.sections import (
    build_sentiment_section,
    build_top_posts,
)
from app.agent_artifacts.canonical import publish_canonical, walk_data_leaves
from app.agent_artifacts.lineage import required_numeric_pointers
from app.agent_artifacts.payloads.brand import BrandData, BrandReportV3, BrandScope
from app.agent_artifacts.payloads.common import iter_null_numeric_paths

SCHEMA_VERSION = "brand_report_v3"

# Evidence 分组键（工具入参 evidence 的合法键）。
GROUP_OVERVIEW_CURRENT = "overview_current"
GROUP_OVERVIEW_MOM = "overview_mom"
GROUP_OVERVIEW_YOY = "overview_yoy"
GROUP_SENTIMENT = "sentiment"
GROUP_DAILY_TREND = "daily_trend"
GROUP_TOPICS = "topics"
GROUP_TOP_POSTS = "top_posts"
GROUP_DIMENSIONS = "dimensions"
GROUP_REGIONS = "regions"

EVIDENCE_GROUPS = (
    GROUP_OVERVIEW_CURRENT,
    GROUP_OVERVIEW_MOM,
    GROUP_OVERVIEW_YOY,
    GROUP_SENTIMENT,
    GROUP_DAILY_TREND,
    GROUP_TOPICS,
    GROUP_TOP_POSTS,
    GROUP_DIMENSIONS,
    GROUP_REGIONS,
)

# payload data 字段顺序（availability 遍历用，与 BrandData 字段一一对应）。
_SECTION_ORDER = (
    "overview",
    "comparisons",
    "sentiment",
    "daily_trend",
    "content_types",
    "creator_tiers",
    "organic_vs_paid",
    "regions",
    "topics",
    "top_posts",
)

_COMPARISON_METRICS = ("total_volume", "total_engagement", "total_posts")
_REGION_LIMIT = 20
_TOPIC_LIMIT = 50


def comparison_windows(
    period: dict[str, Any], mode: str
) -> dict[str, tuple[date, date]]:
    """mom = 紧邻上一等长周期；yoy = 起止各平移一年（2/29→2/28）。

    口径与旧 ``brand_assembler.comparison_windows`` 一致。mode="mom" 时返回值
    不含 yoy 键；period 起止非法（end < start）抛 ``ValueError("invalid_period")``。
    """
    start = parse_date(period.get("start"))
    end = parse_date(period.get("end"))
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


def _as_int(value: float | None) -> int | float | None:
    if value is None:
        return None
    return int(value) if float(value).is_integer() else value


# ---------------------------------------------------------------------------
# overview
# ---------------------------------------------------------------------------


def _overview_platforms(
    rows: list[RowRef],
) -> tuple[dict[str, dict[str, Any]], list[RowRef]]:
    """按平台合并 overview 行；返回 (平台槽位, 参与行)。

    槽位为每个指标分别保留参与行，避免总指标把无关 Evidence 纳入 lineage。
    合计行在存在具名平台行时跳过；只有聚合行时归入 ``all``。
    """
    named: list[RowRef] = []
    aggregate: list[RowRef] = []
    for ref in rows:
        if not has_any(ref.row, VOLUME_KEYS + ENGAGEMENT_KEYS + POSTS_KEYS):
            continue
        platform = canon_platform(first(ref.row, PLATFORM_KEYS))
        (aggregate if platform in AGGREGATE_PLATFORM_NAMES else named).append(ref)
    chosen = named if named else aggregate

    slots: dict[str, dict[str, Any]] = {}
    for ref in chosen:
        platform = canon_platform(first(ref.row, PLATFORM_KEYS))
        slot = slots.setdefault(
            platform,
            {
                "volume": None,
                "engagement": None,
                "posts": None,
                "rows": [],
                "volume_rows": [],
                "engagement_rows": [],
                "posts_rows": [],
            },
        )
        slot["rows"].append(ref)
        for field_name, keys in (
            ("volume", VOLUME_KEYS),
            ("engagement", ENGAGEMENT_KEYS),
            ("posts", POSTS_KEYS),
        ):
            value = num(first(ref.row, keys))
            if value is not None:
                slot[field_name] = (slot[field_name] or 0.0) + value
                slot[f"{field_name}_rows"].append(ref)
    return slots, chosen


def _slot_totals(slots: dict[str, dict[str, Any]]) -> dict[str, Any]:
    totals: dict[str, Any] = {}
    for field_name in ("volume", "engagement", "posts"):
        values = [slot[field_name] for slot in slots.values() if slot[field_name] is not None]
        totals[field_name] = _as_int(sum(values)) if values else None
    return totals


def _metric_rows(slots: dict[str, dict[str, Any]], metric: str) -> list[RowRef]:
    """按指标保序收集真实参与聚合的 Evidence 行。"""
    return [ref for slot in slots.values() for ref in slot[f"{metric}_rows"]]


def _overview_split(rows: list[RowRef]) -> tuple[dict[str, Any], list[RowRef]]:
    """overview 行携带的正/中/负构成汇总（情感兜底数据源）。"""
    split: dict[str, Any] = {"positive": None, "neutral": None, "negative": None}
    contributing: list[RowRef] = []
    for ref in rows:
        used = False
        for name, keys in (
            ("positive", POSITIVE_KEYS),
            ("neutral", NEUTRAL_KEYS),
            ("negative", NEGATIVE_KEYS),
        ):
            value = num(first(ref.row, keys))
            if value is not None:
                split[name] = (split[name] or 0.0) + value
                used = True
        if used:
            contributing.append(ref)
    return split, contributing


def _build_overview(
    rows: list[RowRef],
    platform_scores: dict[str, float | None],
    overall_score: float | None,
    score_rows_by_platform: dict[str, list[RowRef]],
    overall_score_rows: list[RowRef],
    collector: LineageCollector,
) -> tuple[dict[str, Any], bool]:
    slots, chosen = _overview_platforms(rows)
    totals = _slot_totals(slots)
    total_volume = totals["volume"]

    platforms: list[dict[str, Any]] = []
    for platform in sorted(slots, key=platform_sort_key):
        slot = slots[platform]
        volume = _as_int(slot["volume"])
        share = (
            round(slot["volume"] / total_volume, 4)
            if slot["volume"] is not None and total_volume
            else None
        )
        index = len(platforms)
        for field_name in ("volume", "engagement", "posts"):
            value = _as_int(slot[field_name])
            if value is not None:
                collector.add(
                    f"/data/overview/platforms/{index}/{field_name}",
                    slot[f"{field_name}_rows"],
                )
        platform_rows = [
            ref
            for field_name in ("volume", "engagement", "posts")
            for ref in slot[f"{field_name}_rows"]
        ]
        if platform_rows:
            collector.add(f"/data/overview/platforms/{index}/platform", platform_rows)
        if share is not None:
            collector.add(
                f"/data/overview/platforms/{index}/share_of_voice",
                _metric_rows(slots, "volume"),
            )
        platform_score = platform_scores.get(platform)
        if platform_score is not None:
            collector.add(
                f"/data/overview/platforms/{index}/sentiment_score",
                score_rows_by_platform.get(platform, []),
            )
        platforms.append(
            {
                "platform": platform,
                "volume": volume,
                "engagement": _as_int(slot["engagement"]),
                "posts": _as_int(slot["posts"]),
                "share_of_voice": share,
                "sentiment_score": platform_score,
            }
        )

    for field_name, key in (
        ("total_volume", "volume"),
        ("total_engagement", "engagement"),
        ("total_posts", "posts"),
    ):
        if totals[key] is not None:
            collector.add(f"/data/overview/{field_name}", _metric_rows(slots, key))
    if overall_score is not None:
        collector.add("/data/overview/sentiment_score", overall_score_rows)

    overview = {
        "total_volume": totals["volume"],
        "total_engagement": totals["engagement"],
        "total_posts": totals["posts"],
        "sentiment_score": overall_score,
        "platforms": platforms,
    }
    return overview, bool(chosen)


# ---------------------------------------------------------------------------
# comparisons
# ---------------------------------------------------------------------------


def _build_comparison(
    kind: str,
    *,
    requested: bool,
    baseline_window: tuple[date, date] | None,
    timezone: str,
    current_totals: dict[str, Any],
    current_slots: dict[str, dict[str, Any]],
    baseline_rows: list[RowRef],
    collector: LineageCollector,
) -> dict[str, Any]:
    if not requested:
        return {"status": "not_requested", "baseline_period": None, "metrics": []}

    assert baseline_window is not None
    baseline_period = {
        "start": baseline_window[0].isoformat(),
        "end": baseline_window[1].isoformat(),
        "timezone": timezone,
    }
    baseline_slots, baseline_chosen = _overview_platforms(baseline_rows)
    baseline_totals = _slot_totals(baseline_slots)

    metrics: list[dict[str, Any]] = []
    for index, (metric, key) in enumerate(
        zip(
            _COMPARISON_METRICS,
            ("volume", "engagement", "posts"),
            strict=True,
        )
    ):
        current = current_totals[key]
        baseline = baseline_totals[key]
        current_rows = _metric_rows(current_slots, key)
        baseline_metric_rows = _metric_rows(baseline_slots, key)
        delta = None
        rate = None
        if current is not None and baseline is not None:
            delta = _as_int(float(current) - float(baseline))
            rate = round((float(current) - float(baseline)) / float(baseline), 4) if baseline else None
        base = f"/data/comparisons/{kind}/metrics/{index}"
        if current is not None:
            collector.add(f"{base}/current", current_rows)
        if baseline is not None:
            collector.add(f"{base}/baseline", baseline_metric_rows)
        if delta is not None:
            collector.add(f"{base}/delta", [*current_rows, *baseline_metric_rows])
        if rate is not None:
            collector.add(f"{base}/rate", [*current_rows, *baseline_metric_rows])
        metrics.append(
            {
                "metric": metric,
                "current": current,
                "baseline": baseline,
                "delta": delta,
                "rate": rate,
            }
        )

    complete = all(
        metric["current"] is not None and metric["baseline"] is not None
        for metric in metrics
    )
    if complete:
        status = "complete"
    elif not baseline_chosen:
        status = "unavailable"
    else:
        status = "partial"
    return {"status": status, "baseline_period": baseline_period, "metrics": metrics}


# ---------------------------------------------------------------------------
# daily_trend / topics / dimensions / regions
# ---------------------------------------------------------------------------


def _build_daily_trend(
    rows: list[RowRef], collector: LineageCollector
) -> tuple[list[dict[str, Any]], bool]:
    buckets: dict[tuple[date, str], dict[str, Any]] = {}
    for ref in rows:
        day = parse_date(first(ref.row, TIME_KEYS))
        if day is None:
            continue  # 无日期行（常为合计行）按旧口径静默跳过
        platform = canon_platform(first(ref.row, PLATFORM_KEYS))
        slot = buckets.setdefault(
            (day, platform),
            {
                "volume": None,
                "engagement": None,
                "positive": None,
                "neutral": None,
                "negative": None,
                "identity_rows": [],
                "volume_rows": [],
                "engagement_rows": [],
                "positive_rows": [],
                "neutral_rows": [],
                "negative_rows": [],
            },
        )
        slot["identity_rows"].append(ref)
        for field_name, keys in (
            ("volume", VOLUME_KEYS),
            ("engagement", ENGAGEMENT_KEYS),
            ("positive", POSITIVE_KEYS),
            ("neutral", NEUTRAL_KEYS),
            ("negative", NEGATIVE_KEYS),
        ):
            value = num(first(ref.row, keys))
            if value is not None:
                slot[field_name] = (slot[field_name] or 0.0) + value
                slot[f"{field_name}_rows"].append(ref)

    items: list[dict[str, Any]] = []
    for day, platform in sorted(buckets, key=lambda key: (key[0], platform_sort_key(key[1]))):
        slot = buckets[(day, platform)]
        index = len(items)
        collector.add(f"/data/daily_trend/{index}/date", slot["identity_rows"])
        collector.add(f"/data/daily_trend/{index}/platform", slot["identity_rows"])
        for field_name in ("volume", "engagement", "positive", "neutral", "negative"):
            value = _as_int(slot[field_name])
            if value is not None:
                collector.add(
                    f"/data/daily_trend/{index}/{field_name}",
                    slot[f"{field_name}_rows"],
                )
        items.append(
            {
                "date": day.isoformat(),
                "platform": platform,
                **{name: _as_int(slot[name]) for name in ("volume", "engagement", "positive", "neutral", "negative")},
            }
        )
    return items, bool(items)


def _build_topics(
    rows: list[RowRef], collector: LineageCollector
) -> tuple[list[dict[str, Any]], bool]:
    merged: dict[str, dict[str, Any]] = {}
    for ref in rows:
        topic = text(first(ref.row, TOPIC_KEYS))
        if topic is None:
            continue
        slot = merged.setdefault(
            topic,
            {
                "volume": None,
                "engagement": None,
                "positive": None,
                "neutral": None,
                "negative": None,
                "rows": [],
            },
        )
        slot["rows"].append(ref)
        for field_name, keys in (
            ("volume", VOLUME_KEYS),
            ("engagement", ENGAGEMENT_KEYS),
            ("positive", POSITIVE_KEYS),
            ("neutral", NEUTRAL_KEYS),
            ("negative", NEGATIVE_KEYS),
        ):
            value = num(first(ref.row, keys))
            if value is not None:
                slot[field_name] = (slot[field_name] or 0.0) + value

    ordered = sorted(
        merged.items(),
        key=lambda item: (item[1]["volume"] is None, -(item[1]["volume"] or 0.0), item[0]),
    )[:_TOPIC_LIMIT]
    items: list[dict[str, Any]] = []
    for topic, slot in ordered:
        score = sentiment_score(slot["positive"], slot["neutral"], slot["negative"])
        index = len(items)
        for field_name in ("volume", "engagement"):
            value = _as_int(slot[field_name])
            if value is not None:
                collector.add(f"/data/topics/{index}/{field_name}", slot["rows"])
        if score is not None:
            collector.add(f"/data/topics/{index}/sentiment_score", slot["rows"])
        items.append(
            {
                "topic": topic,
                "volume": _as_int(slot["volume"]),
                "engagement": _as_int(slot["engagement"]),
                "sentiment_score": score,
            }
        )
    return items, bool(items)


def _build_dimension_rows(
    rows: list[RowRef],
    label_keys: tuple[str, ...],
    section: str,
    label_field: str,
    extra_fields: tuple[str, ...],
    collector: LineageCollector,
) -> tuple[list[dict[str, Any]], bool]:
    """content_types / creator_tiers 共用的「平台 + 标签 + 声量」归一。"""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for ref in rows:
        label = text(first(ref.row, label_keys))
        if label is None:
            continue
        platform = canon_platform(first(ref.row, PLATFORM_KEYS))
        key = (platform, label)
        slot = merged.setdefault(key, {"volume": None, "rows": []})
        slot["rows"].append(ref)
        value = num(first(ref.row, VOLUME_KEYS))
        if value is not None:
            slot["volume"] = (slot["volume"] or 0.0) + value

    items: list[dict[str, Any]] = []
    for (platform, label), slot in sorted(
        merged.items(), key=lambda item: (platform_sort_key(item[0][0]), item[0][1])
    ):
        index = len(items)
        if slot["volume"] is not None:
            collector.add(f"/data/{section}/{index}/volume", slot["rows"])
        item = {"platform": platform, label_field: label, "volume": _as_int(slot["volume"])}
        for field_name in extra_fields:
            item[field_name] = None
        items.append(item)
    return items, bool(items)


def _build_organic_vs_paid(
    rows: list[RowRef], collector: LineageCollector
) -> tuple[list[dict[str, Any]], bool]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for ref in rows:
        kind = commercial_kind(first(ref.row, COMMERCIAL_KEYS))
        volume = num(first(ref.row, VOLUME_KEYS))
        if kind is None or volume is None:
            continue
        platform = canon_platform(first(ref.row, PLATFORM_KEYS))
        slot = merged.setdefault((platform, kind), {"volume": None, "rows": []})
        slot["rows"].append(ref)
        slot["volume"] = (slot["volume"] or 0.0) + volume

    items: list[dict[str, Any]] = []
    for (platform, kind), slot in sorted(
        merged.items(), key=lambda item: (platform_sort_key(item[0][0]), item[0][1])
    ):
        index = len(items)
        collector.add(f"/data/organic_vs_paid/{index}/volume", slot["rows"])
        items.append(
            {
                "platform": platform,
                "kind": kind,
                "posts": None,
                "volume": _as_int(slot["volume"]),
                "engagement": None,
            }
        )
    return items, bool(items)


def _build_regions(
    rows: list[RowRef], collector: LineageCollector
) -> tuple[list[dict[str, Any]], bool]:
    merged: dict[str, dict[str, Any]] = {}
    for ref in rows:
        # 映射形态：{"地域分布": {"广东": 123, ...}}
        for map_key in REGION_MAP_KEYS:
            mapping = ref.row.get(map_key)
            if isinstance(mapping, dict):
                for region, raw_value in mapping.items():
                    value = num(raw_value)
                    if value is None:
                        continue
                    slot = merged.setdefault(str(region), {"volume": None, "rows": []})
                    slot["volume"] = (slot["volume"] or 0.0) + value
                    slot["rows"].append(ref)
        region = text(first(ref.row, REGION_KEYS))
        if region is None:
            continue
        value = num(first(ref.row, VOLUME_KEYS))
        if value is None:
            continue
        slot = merged.setdefault(region, {"volume": None, "rows": []})
        slot["volume"] = (slot["volume"] or 0.0) + value
        slot["rows"].append(ref)

    total = sum(
        slot["volume"] for slot in merged.values() if slot["volume"] is not None
    )
    ordered = sorted(
        merged.items(),
        key=lambda item: (item[1]["volume"] is None, -(item[1]["volume"] or 0.0), item[0]),
    )[:_REGION_LIMIT]
    items: list[dict[str, Any]] = []
    for region, slot in ordered:
        share = (
            round(slot["volume"] / total, 4)
            if slot["volume"] is not None and total
            else None
        )
        index = len(items)
        if slot["volume"] is not None:
            collector.add(f"/data/regions/{index}/volume", slot["rows"])
        if share is not None:
            collector.add(f"/data/regions/{index}/share", slot["rows"])
        items.append(
            {
                "region": region,
                "volume": _as_int(slot["volume"]),
                "share": share,
                "sentiment_score": None,
            }
        )
    return items, bool(items)


# ---------------------------------------------------------------------------
# 叙事与受限披露
# ---------------------------------------------------------------------------


def _default_narrative(brand: str, data_status: str) -> dict[str, Any]:
    status_text = "完整" if data_status == "complete" else "受限（详见限制披露）"
    return {
        "executive_summary": f"{brand}品牌分析报告已生成，数据状态：{status_text}。",
        "findings": [],
        "recommendations": [],
    }


def _assemble_availability(
    data_model: BrandData,
    *,
    has_rows: dict[str, bool],
    extra: dict[str, list[dict[str, Any]]],
    force_partial: set[str],
) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    """按 B1 递归 null 治理推导 availability/limitations/data_status。"""
    availability: dict[str, Any] = {}
    limitations: list[dict[str, Any]] = []
    for section in _SECTION_ORDER:
        field = BrandData.model_fields[section]
        nulls = list(
            iter_null_numeric_paths(
                field.annotation, getattr(data_model, section), section
            )
        )
        section_limitations = list(extra.get(section) or ())
        if not has_rows.get(section, False) and section not in force_partial:
            status = "unavailable"
            reason_codes = ["no_evidence"]
            section_limitations.append(
                {
                    "code": "no_evidence",
                    "message": f"章节 {section} 未提供 Evidence，数据不可用",
                    "affected_paths": [section],
                }
            )
        elif nulls or section in force_partial:
            status = "partial"
            reason_codes = ["metric_data_missing"]
            if nulls:
                section_limitations.append(
                    {
                        "code": "metric_data_missing",
                        "message": f"章节 {section} 部分数值缺失，数据受限披露",
                        "affected_paths": nulls,
                    }
                )
        else:
            status = "complete"
            reason_codes = []
        availability[section] = {"status": status, "reason_codes": reason_codes}
        limitations.extend(section_limitations)

    restricted = any(
        availability[section]["status"] != "complete"
        for section in BrandReportV3.REQUIRED_SECTIONS
    )
    return ("restricted" if restricted else "complete"), availability, limitations


# ---------------------------------------------------------------------------
# 公开入口
# ---------------------------------------------------------------------------


def build_brand_report_draft(
    *,
    scope: dict[str, Any],
    evidence: dict[str, list[tuple[str, Any]]],
    narrative: dict[str, Any] | None = None,
    top_posts_limit: int = 20,
    data_as_of: Any = None,
    source_names: tuple[str, ...] = ("brand_evidence",),
) -> DraftBuildResult:
    """把模型选定的章节 Evidence 转换为 ``brand_report_v3`` Draft。

    ``evidence`` 按章节分组（``overview_current`` / ``overview_mom`` /
    ``overview_yoy`` / ``sentiment`` / ``daily_trend`` / ``topics`` /
    ``top_posts`` / ``dimensions`` / ``regions``），值为
    ``(evidence_id, raw_payload)`` 列表。必需章节 Evidence 缺失时产出
    restricted 产物并披露 limitation；全部章节都提取不到行时抛
    :class:`DraftBuildError`（由工具层结构化回喂模型）。
    """
    try:
        scope_model = BrandScope.model_validate(scope)
    except ValidationError as exc:
        raise DraftBuildError(f"invalid brand scope: {exc}") from exc
    try:
        reject_exclusive_group_evidence_reuse(
            evidence,
            (GROUP_OVERVIEW_CURRENT, GROUP_OVERVIEW_MOM, GROUP_OVERVIEW_YOY),
        )
    except ValueError as exc:
        raise DraftBuildError(str(exc)) from exc

    canonical = canonicalize_marketing_evidence(
        {group: evidence.get(group, []) for group in EVIDENCE_GROUPS}
    )
    groups = {group: canonical.rows(group) for group in EVIDENCE_GROUPS}
    if not any(groups.values()):
        raise DraftBuildError(
            "build_brand_report_draft requires at least one usable evidence row"
        )

    collector = LineageCollector()

    # ---- sentiment（先于 overview：平台 sentiment_score 依赖它） ----
    sentiment, sentiment_has_rows = build_sentiment_section(
        groups[GROUP_SENTIMENT], collector
    )
    force_partial: set[str] = set()
    extra_limitations: dict[str, list[dict[str, Any]]] = {}
    sentiment_score_rows = [
        ref
        for ref in groups[GROUP_SENTIMENT]
        if first(ref.row, SENTIMENT_KEYS) is not None
    ]

    # overview 行情感构成（兜底 + overview.sentiment_score 数据源）。
    overview_split, split_rows = _overview_split(groups[GROUP_OVERVIEW_CURRENT])

    if not sentiment_has_rows and any(
        value is not None for value in overview_split.values()
    ):
        # 情感兜底：overview 汇总构成（platform 明细不可得 → partial 披露）。
        total = sum(value for value in overview_split.values() if value is not None)
        summary = {}
        for name in ("positive", "neutral", "negative"):
            value = overview_split[name]
            summary[name] = {
                "count": _as_int(value),
                "share": round(value / total, 4) if value is not None and total else None,
            }
        sentiment = {"summary": summary, "by_platform": []}
        sentiment_has_rows = True
        force_partial.add("sentiment")
        extra_limitations.setdefault("sentiment", []).append(
            {
                "code": "sentiment_from_overview",
                "message": "情感明细未采集，构成来自综合概览汇总，无平台拆分",
                "affected_paths": ["sentiment"],
            }
        )
        for name in ("positive", "neutral", "negative"):
            if summary[name]["count"] is not None:
                collector.add(f"/data/sentiment/summary/{name}/count", split_rows)
                collector.add(f"/data/sentiment/summary/{name}/share", split_rows)

    # 情感计分来源行：明细优先，兜底用 overview 构成行。
    if not sentiment_score_rows:
        sentiment_score_rows = split_rows
    sentiment_coverage_rows = groups[GROUP_SENTIMENT] or split_rows

    summary_counts = sentiment["summary"]
    overall_score = sentiment_score(
        summary_counts["positive"]["count"],
        summary_counts["neutral"]["count"],
        summary_counts["negative"]["count"],
    ) if sentiment_has_rows else None
    platform_scores: dict[str, float | None] = {}
    platform_score_rows: dict[str, list[RowRef]] = {}
    for ref in sentiment_score_rows:
        if first(ref.row, SENTIMENT_KEYS) is not None:
            platform_score_rows.setdefault(canon_platform(first(ref.row, PLATFORM_KEYS)), []).append(ref)
    for entry in sentiment["by_platform"]:
        platform_scores[entry["platform"]] = sentiment_score(
            entry["positive"]["count"], entry["neutral"]["count"], entry["negative"]["count"]
        )

    # ---- overview ----
    overview, overview_has_rows = _build_overview(
        groups[GROUP_OVERVIEW_CURRENT],
        platform_scores,
        overall_score,
        platform_score_rows,
        sentiment_score_rows,
        collector,
    )
    overview_slots, _overview_chosen = _overview_platforms(groups[GROUP_OVERVIEW_CURRENT])
    current_totals = _slot_totals(overview_slots)

    # ---- comparisons ----
    mode = scope_model.comparison_mode
    try:
        windows = comparison_windows(
            scope_model.period.model_dump(), "mom_yoy" if mode == "mom_yoy" else "mom"
        )
    except ValueError as exc:
        raise DraftBuildError(f"invalid_period: {exc}") from exc
    comparisons = {
        kind: _build_comparison(
            kind,
            requested=(mode == "mom_yoy" or (mode == "mom" and kind == "mom")),
            baseline_window=windows.get(kind),
            timezone=scope_model.period.timezone,
            current_totals=current_totals,
            current_slots=overview_slots,
            baseline_rows=groups[GROUP_OVERVIEW_MOM if kind == "mom" else GROUP_OVERVIEW_YOY],
            collector=collector,
        )
        for kind in ("mom", "yoy")
    }

    # ---- 其余章节 ----
    daily_trend, trend_has_rows = _build_daily_trend(groups[GROUP_DAILY_TREND], collector)
    topics, topics_has_rows = _build_topics(groups[GROUP_TOPICS], collector)
    top_posts, posts_meta = build_top_posts(
        groups[GROUP_TOP_POSTS], collector, limit=min(max(top_posts_limit, 1), 20)
    )
    if posts_meta["skipped"]:
        force_partial.add("top_posts")
        extra_limitations.setdefault("top_posts", []).append(
            {
                "code": "post_row_incomplete",
                "message": "部分热帖缺少帖子 ID 或发布时间，已跳过",
                "affected_paths": ["top_posts"],
            }
        )
    if posts_meta["missing_platform"]:
        force_partial.add("top_posts")
        extra_limitations.setdefault("top_posts", []).append(
            {
                "code": "post_platform_missing",
                "message": "部分热帖缺少平台，已跳过以避免伪造 all 平台",
                "affected_paths": ["top_posts"],
            }
        )
    if posts_meta["missing_url"]:
        force_partial.add("top_posts")
        extra_limitations.setdefault("top_posts", []).append(
            {
                "code": "post_url_missing",
                "message": "部分热帖缺少原帖链接，前端展示不可用",
                "affected_paths": ["top_posts"],
            }
        )
    for key, code, label in (
        ("missing_title", "post_title_missing", "标题"),
        ("missing_author", "post_author_missing", "作者"),
    ):
        if posts_meta[key]:
            force_partial.add("top_posts")
            extra_limitations.setdefault("top_posts", []).append(
                {
                    "code": code,
                    "message": f"部分热帖缺少{label}，已保留为不可用字段",
                    "affected_paths": ["top_posts"],
                }
            )

    content_types, content_has_rows = _build_dimension_rows(
        groups[GROUP_DIMENSIONS],
        CONTENT_TYPE_KEYS,
        "content_types",
        "type",
        ("posts", "engagement"),
        collector,
    )
    creator_tiers, tiers_has_rows = _build_dimension_rows(
        groups[GROUP_DIMENSIONS],
        TIER_KEYS,
        "creator_tiers",
        "tier",
        ("creator_count", "posts", "engagement"),
        collector,
    )
    organic_vs_paid, organic_has_rows = _build_organic_vs_paid(
        groups[GROUP_DIMENSIONS], collector
    )
    regions, regions_has_rows = _build_regions(groups[GROUP_REGIONS], collector)

    data = {
        "overview": overview,
        "comparisons": comparisons,
        "sentiment": sentiment,
        "daily_trend": daily_trend,
        "content_types": content_types,
        "creator_tiers": creator_tiers,
        "organic_vs_paid": organic_vs_paid,
        "regions": regions,
        "topics": topics,
        "top_posts": top_posts,
    }
    try:
        data_model = BrandData.model_validate(data)
    except ValidationError as exc:
        raise DraftBuildError(f"invalid brand data: {exc}") from exc
    data = data_model.model_dump(mode="json")

    has_rows = {
        "overview": overview_has_rows,
        # comparisons 无独立 Evidence 概念：未请求时恒 complete（无 null 叶子）。
        "comparisons": True,
        "sentiment": sentiment_has_rows,
        "daily_trend": trend_has_rows,
        "content_types": content_has_rows,
        "creator_tiers": tiers_has_rows,
        "organic_vs_paid": organic_has_rows,
        "regions": regions_has_rows,
        "topics": topics_has_rows,
        "top_posts": bool(top_posts),
    }
    # ---- 平台覆盖率：按章节实际 Evidence 判断，不把已观测原始值误标 partial ----
    partial_paths: set[str] = set()
    def mark_coverage_partial(
        section: str, rows: list[RowRef], *, paths: list[str] | None = None
    ) -> bool:
        if not has_rows.get(section, False) or not platform_coverage_incomplete(
            scope_model.platforms, rows
        ):
            return False
        force_partial.add(section)
        partial_paths.update(paths or ())
        extra_limitations.setdefault(section, []).append(
            {
                "code": "platform_coverage_incomplete",
                "message": "scope 声明的部分平台没有覆盖，按可得平台数据受限披露",
                "affected_paths": [section],
            }
        )
        return True

    mark_coverage_partial(
        "overview",
        groups[GROUP_OVERVIEW_CURRENT],
        paths=[
            "/data/overview/total_volume",
            "/data/overview/total_engagement",
            "/data/overview/total_posts",
        ],
    )
    mark_coverage_partial(
        "sentiment",
        sentiment_coverage_rows,
        paths=[
            path
            for path, value in walk_data_leaves(data)
            if path.startswith("/data/sentiment/summary/") and value is not None
        ],
    )
    mark_coverage_partial("daily_trend", groups[GROUP_DAILY_TREND])
    mark_coverage_partial("top_posts", groups[GROUP_TOP_POSTS])
    if mode != "none":
        mark_coverage_partial(
            "comparisons",
            groups[GROUP_OVERVIEW_CURRENT],
            paths=[
                path
                for path, value in walk_data_leaves(data)
                if path.startswith("/data/comparisons/")
                and path.rsplit("/", 1)[-1] in {"current", "delta", "rate"}
                and value is not None
            ],
        )

    data_status, availability, limitations = _assemble_availability(
        data_model,
        has_rows=has_rows,
        extra=extra_limitations,
        force_partial=force_partial,
    )
    for section, section_limitations in extra_limitations.items():
        if any(item["code"] == "platform_coverage_incomplete" for item in section_limitations):
            availability[section]["reason_codes"] = [
                *availability[section]["reason_codes"],
                "platform_coverage_incomplete",
            ]

    refs = collector.build()
    try:
        canonical_fields, field_lineage = publish_canonical(
            data, refs, partial_paths=frozenset(partial_paths), module="brand"
        )
    except ValidationError as exc:
        raise DraftBuildError(f"invalid brand canonical publication: {exc}") from exc

    payload = {
        "schema_version": SCHEMA_VERSION,
        "module": "brand",
        "data_status": data_status,
        "availability": availability,
        "limitations": limitations,
        "methodology": methodology_dict(data_as_of=data_as_of, source_names=source_names),
        "scope": scope_model.model_dump(),
        "data": data,
        "narrative": narrative if narrative is not None else _default_narrative(
            scope_model.brand, data_status
        ),
        "canonical_data": tuple(canonical_fields),
        "field_lineage": field_lineage,
    }
    try:
        BrandReportV3.model_validate(payload)  # fail-fast：builder 输出必须合法。
    except ValidationError as exc:
        raise DraftBuildError(f"invalid brand_report_v3 payload: {exc}") from exc
    missing = required_numeric_pointers(payload) - {ref["artifact_path"] for ref in refs}
    if missing:
        raise DraftBuildError(
            "brand builder lineage coverage incomplete: " + ", ".join(sorted(missing))
        )

    return DraftBuildResult(
        module="brand",
        schema_version=SCHEMA_VERSION,
        artifact_type=SCHEMA_VERSION,
        business_fields={"brand": scope_model.brand},
        payload=payload,
        evidence_refs=refs,
    )


__all__ = [
    "EVIDENCE_GROUPS",
    "SCHEMA_VERSION",
    "build_brand_report_draft",
    "comparison_windows",
]
