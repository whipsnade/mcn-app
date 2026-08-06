"""``campaign_report_v2`` Draft builder（设计 §12.1 / v3 加固 §3.3/§6.1，B2）。

模型提供用户确认的 scope（brand/campaign/period/platforms/keywords）、按章节
分组的 Evidence（``posts`` 原始帖行 + 可选 ``sentiment`` 情感明细行）与叙事
字段；builder 负责确定性聚合、字段级 lineage 与强类型 payload 构造。builder
不选择 MCP 工具、不发起外部查询、不改变用户目标（§3.3 红线）。

旧代码中活动报告为模型直出（``campaign_analysis_v1``），无确定性聚合真源；
本 builder 的口径与品牌 builder / 旧 ``brand_assembler`` 的字段归一保持一致：

- overview：``total_volume`` = 声量（帖子行数，声量与发帖同口径，与旧
  assembler 的「发帖数」别名一致）；``total_engagement`` = 行互动量求和
  （互动字段缺失时按赞/评/转/藏求和）；``total_creators`` = 去重作者数；
  ``sentiment_score`` 为净情感指数 ``(正面 - 负面) / 总量 * 100``；
- platform_contributions：按平台分组，``share`` = 平台声量/总声量（4 位）；
- timeline：按 (日期, 平台) 分组；无日期行按旧口径静默跳过（常为合计行）；
- kol_contributions：按 (平台, 作者 uid) 分组（uid 缺失回退昵称），
  ``contribution_share`` = 达人互动量/全名单互动量合计（4 位），互动量降序
  Top20；
- sentiment：优先消费 ``sentiment`` 分组明细行；缺失时用 posts 行的情感
  字段（每帖计 1）；
- top_posts：与品牌 builder 共用（互动量降序 Top20）。

缺失数值保持 null 并按要求 partial/unavailable + limitation（B1 递归 null
治理），绝不把缺失当 0。
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import ValidationError

from app.agent_artifacts.builders.common import (
    DraftBuildError,
    DraftBuildResult,
    LineageCollector,
    methodology_dict,
)
from app.agent_artifacts.builders.raw_rows import (
    AUTHOR_ID_KEYS,
    AUTHOR_KEYS,
    COLLECT_KEYS,
    COMMENT_KEYS,
    CONTENT_TYPE_KEYS,
    DATE_KEYS,
    ENGAGEMENT_KEYS,
    LIKE_KEYS,
    PLATFORM_KEYS,
    POST_DATE_KEYS,
    REGION_KEYS,
    SENTIMENT_KEYS,
    SHARE_KEYS,
    VOLUME_KEYS,
    RowRef,
    canon_platform,
    extract_rows,
    first,
    parse_date,
    platform_sort_key,
    sentiment_score,
    text,
    whole,
)
from app.agent_artifacts.builders.sections import (
    build_sentiment_section,
    build_top_posts,
)
from app.agent_artifacts.lineage import required_numeric_pointers
from app.agent_artifacts.payloads.campaign import (
    CampaignData,
    CampaignReportV2,
    CampaignScope,
)
from app.agent_artifacts.payloads.common import iter_null_numeric_paths

SCHEMA_VERSION = "campaign_report_v2"

# Evidence 分组键（工具入参 evidence 的合法键）。posts/sentiment 为既有分组；
# Gate C Task 4 增加 current/baseline/post（周期对比）/ social（社媒）/ upload
# （用户补充资料：成本/转化/内部指标）。
GROUP_POSTS = "posts"
GROUP_SENTIMENT = "sentiment"
GROUP_CURRENT = "current"
GROUP_BASELINE = "baseline"
GROUP_POST = "post"
GROUP_SOCIAL = "social"
GROUP_UPLOAD = "upload"

EVIDENCE_GROUPS = (
    GROUP_POSTS,
    GROUP_SENTIMENT,
    GROUP_CURRENT,
    GROUP_BASELINE,
    GROUP_POST,
    GROUP_SOCIAL,
    GROUP_UPLOAD,
)

_SECTION_ORDER = (
    "overview",
    "platform_contributions",
    "timeline",
    "kol_contributions",
    "content_types",
    "sentiment",
    "top_posts",
    "comparisons",
    "attribution",
    "organic_summary",
    "audience_regions",
    "internal_metrics",
    "roi",
)

_KOL_LIMIT = 20

# 用户补充资料（upload）行的成本/转化/内部指标键。
SPEND_KEYS = ("投放金额", "花费", "消耗", "成本", "spend", "cost")
IMPRESSION_KEYS = ("曝光", "曝光数", "展示", "impressions", "views")
CONVERSION_KEYS = ("转化", "转化数", "转化量", "成交数", "conversions", "conversion")
REVENUE_KEYS = ("销售额", "销售金额", "收入", "GMV", "revenue", "sales")
# 帖子的付费/自然归属键。
ATTRIBUTION_KEYS = ("归属", "是否付费", "投放类型", "付费/自然", "attribution")
# 归属语义字段分流（Gate C 第三轮）：布尔字段的值直接表达付费与否
# （是/否、true/false、1/0、yes/no）；文本字段的值用标准化 token 匹配。
_BOOLEAN_ATTRIBUTION_KEYS = ("是否付费",)
_TEXT_ATTRIBUTION_KEYS = ("归属", "投放类型", "付费/自然", "attribution")
_PAID_BOOL_VALUES = frozenset({"是", "true", "1", "yes"})
_ORGANIC_BOOL_VALUES = frozenset({"否", "false", "0", "no"})


def _extract_group(pairs: list[tuple[str, Any]] | None) -> list[RowRef]:
    rows: list[RowRef] = []
    for evidence_id, raw_payload in pairs or ():
        rows.extend(extract_rows(evidence_id, raw_payload))
    return rows


def _row_engagement(row: dict[str, Any]) -> int | None:
    """行互动量：互动字段优先，缺失时按赞/评/转/藏求和；都无则 None。"""
    engagement = whole(first(row, ENGAGEMENT_KEYS))
    if engagement is not None:
        return engagement
    parts = [
        value
        for value in (
            whole(first(row, LIKE_KEYS)),
            whole(first(row, COMMENT_KEYS)),
            whole(first(row, SHARE_KEYS)),
            whole(first(row, COLLECT_KEYS)),
        )
        if value is not None
    ]
    return sum(parts) if parts else None


def _row_author_id(row: dict[str, Any]) -> str | None:
    """达人身份：作者 uid 键优先，缺失回退昵称；都没有返回 None。"""
    return text(first(row, AUTHOR_ID_KEYS)) or text(first(row, AUTHOR_KEYS))


def _row_author_name(row: dict[str, Any]) -> str:
    return text(first(row, AUTHOR_KEYS)) or ""


# ---------------------------------------------------------------------------
# 章节装配
# ---------------------------------------------------------------------------


def _group_posts(rows: list[RowRef]) -> list[RowRef]:
    """参与统计的帖子行：至少要能识别为一篇帖子（id/标题/链接/作者/互动任一）。"""
    return rows


def _dedup_rows(rows: list[RowRef]) -> list[RowRef]:
    """按 (evidence_id, source_path) 去重保序。

    同一 Evidence 行被 posts/social/current 等多个分组引用时只参与一次聚合，
    attribution/organic/audience/comparison 绝不重复计算（Gate C 复审）。
    """
    seen: set[tuple[str, str]] = set()
    unique: list[RowRef] = []
    for ref in rows:
        key = (ref.evidence_id, ref.source_path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(ref)
    return unique


def _build_overview(
    rows: list[RowRef],
    score: float | None,
    score_rows: list[RowRef],
    collector: LineageCollector,
) -> tuple[dict[str, Any], bool]:
    has_rows = bool(rows)
    total_posts = len(rows) if has_rows else None
    engagements = [(ref, _row_engagement(ref.row)) for ref in rows]
    engagement_rows = [ref for ref, value in engagements if value is not None]
    total_engagement = (
        sum(value for _, value in engagements if value is not None) if engagement_rows else None
    )
    authors = {_row_author_id(ref.row) for ref in rows}
    authors.discard(None)
    total_creators = len(authors) if authors else None

    if has_rows:
        collector.add("/data/overview/total_volume", rows)
        collector.add("/data/overview/total_posts", rows)
    if total_engagement is not None:
        collector.add("/data/overview/total_engagement", engagement_rows)
    if total_creators is not None:
        collector.add("/data/overview/total_creators", rows)
    if score is not None:
        collector.add("/data/overview/sentiment_score", score_rows)

    return (
        {
            "total_volume": total_posts,
            "total_engagement": total_engagement,
            "total_posts": total_posts,
            "total_creators": total_creators,
            "sentiment_score": score,
        },
        has_rows,
    )


def _build_platform_contributions(
    rows: list[RowRef], collector: LineageCollector
) -> tuple[list[dict[str, Any]], bool]:
    buckets: dict[str, list[RowRef]] = {}
    for ref in rows:
        platform = canon_platform(first(ref.row, PLATFORM_KEYS))
        buckets.setdefault(platform, []).append(ref)
    total = len(rows)

    items: list[dict[str, Any]] = []
    for platform in sorted(buckets, key=platform_sort_key):
        bucket = buckets[platform]
        engagements = [(ref, _row_engagement(ref.row)) for ref in bucket]
        engagement_rows = [ref for ref, value in engagements if value is not None]
        engagement = (
            sum(value for _, value in engagements if value is not None)
            if engagement_rows
            else None
        )
        authors = {_row_author_id(ref.row) for ref in bucket}
        authors.discard(None)
        creators = len(authors) if authors else None
        share = round(len(bucket) / total, 4) if total else None

        index = len(items)
        base = f"/data/platform_contributions/{index}"
        collector.add(f"{base}/volume", bucket)
        collector.add(f"{base}/posts", bucket)
        if engagement is not None:
            collector.add(f"{base}/engagement", engagement_rows)
        if creators is not None:
            collector.add(f"{base}/creators", bucket)
        if share is not None:
            collector.add(f"{base}/share", rows)
        items.append(
            {
                "platform": platform,
                "volume": len(bucket),
                "engagement": engagement,
                "posts": len(bucket),
                "creators": creators,
                "share": share,
            }
        )
    return items, bool(items)


def _build_timeline(
    rows: list[RowRef], collector: LineageCollector
) -> tuple[list[dict[str, Any]], bool]:
    buckets: dict[tuple[Any, str], list[RowRef]] = {}
    for ref in rows:
        # 趋势日期键优先，帖子行回退发布时间键（口径同旧 assembler 的日期别名）。
        day = parse_date(first(ref.row, DATE_KEYS + POST_DATE_KEYS))
        if day is None:
            continue  # 无日期行（常为合计行）按旧口径静默跳过
        platform = canon_platform(first(ref.row, PLATFORM_KEYS))
        buckets.setdefault((day, platform), []).append(ref)

    items: list[dict[str, Any]] = []
    for day, platform in sorted(buckets, key=lambda key: (key[0], platform_sort_key(key[1]))):
        bucket = buckets[(day, platform)]
        engagements = [(ref, _row_engagement(ref.row)) for ref in bucket]
        engagement_rows = [ref for ref, value in engagements if value is not None]
        engagement = (
            sum(value for _, value in engagements if value is not None)
            if engagement_rows
            else None
        )
        index = len(items)
        base = f"/data/timeline/{index}"
        collector.add(f"{base}/volume", bucket)
        collector.add(f"{base}/posts", bucket)
        if engagement is not None:
            collector.add(f"{base}/engagement", engagement_rows)
        items.append(
            {
                "date": day.isoformat(),
                "platform": platform,
                "volume": len(bucket),
                "engagement": engagement,
                "posts": len(bucket),
            }
        )
    return items, bool(items)


def _build_kol_contributions(
    rows: list[RowRef], collector: LineageCollector
) -> tuple[list[dict[str, Any]], bool]:
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for ref in rows:
        uid = _row_author_id(ref.row)
        if uid is None:
            continue
        platform = canon_platform(first(ref.row, PLATFORM_KEYS))
        slot = buckets.setdefault(
            (platform, uid), {"rows": [], "nickname": _row_author_name(ref.row)}
        )
        slot["rows"].append(ref)

    def _engagement(bucket: list[RowRef]) -> int | None:
        values = [_row_engagement(ref.row) for ref in bucket]
        known = [value for value in values if value is not None]
        return sum(known) if known else None

    total_engagement = sum(
        value
        for slot in buckets.values()
        if (value := _engagement(slot["rows"])) is not None
    )

    ordered = sorted(
        buckets.items(),
        key=lambda item: (
            _engagement(item[1]["rows"]) is None,
            -(_engagement(item[1]["rows"]) or 0),
            platform_sort_key(item[0][0]),
            item[0][1],
        ),
    )[:_KOL_LIMIT]

    items: list[dict[str, Any]] = []
    for (platform, uid), slot in ordered:
        engagement = _engagement(slot["rows"])
        share = (
            round(engagement / total_engagement, 4)
            if engagement is not None and total_engagement
            else None
        )
        index = len(items)
        base = f"/data/kol_contributions/{index}"
        collector.add(f"{base}/posts", slot["rows"])
        collector.add(f"{base}/volume", slot["rows"])
        if engagement is not None:
            collector.add(f"{base}/engagement", slot["rows"])
        if share is not None:
            collector.add(
                f"{base}/contribution_share",
                [ref for entry in buckets.values() for ref in entry["rows"]],
            )
        items.append(
            {
                "platform": platform,
                "kol_uid": uid,
                "nickname": slot["nickname"],
                "posts": len(slot["rows"]),
                "volume": len(slot["rows"]),
                "engagement": engagement,
                "contribution_share": share,
            }
        )
    return items, bool(items)


def _build_content_types(
    rows: list[RowRef], collector: LineageCollector
) -> tuple[list[dict[str, Any]], bool]:
    buckets: dict[tuple[str, str], list[RowRef]] = {}
    for ref in rows:
        label = text(first(ref.row, CONTENT_TYPE_KEYS))
        if label is None:
            continue
        platform = canon_platform(first(ref.row, PLATFORM_KEYS))
        buckets.setdefault((platform, label), []).append(ref)

    items: list[dict[str, Any]] = []
    for (platform, label), bucket in sorted(
        buckets.items(), key=lambda item: (platform_sort_key(item[0][0]), item[0][1])
    ):
        engagements = [(ref, _row_engagement(ref.row)) for ref in bucket]
        engagement_rows = [ref for ref, value in engagements if value is not None]
        engagement = (
            sum(value for _, value in engagements if value is not None)
            if engagement_rows
            else None
        )
        index = len(items)
        base = f"/data/content_types/{index}"
        collector.add(f"{base}/posts", bucket)
        collector.add(f"{base}/volume", bucket)
        if engagement is not None:
            collector.add(f"{base}/engagement", engagement_rows)
        items.append(
            {
                "platform": platform,
                "type": label,
                "posts": len(bucket),
                "volume": len(bucket),
                "engagement": engagement,
            }
        )
    return items, bool(items)


# ---------------------------------------------------------------------------
# 叙事与受限披露
# ---------------------------------------------------------------------------


def _default_narrative(brand: str, campaign: str, data_status: str) -> dict[str, Any]:
    status_text = "完整" if data_status == "complete" else "受限（详见限制披露）"
    return {
        "executive_summary": f"{brand}「{campaign}」活动分析报告已生成，数据状态：{status_text}。",
        "phase_review": [],
        "findings": [],
        "recommendations": [],
    }


def _assemble_availability(
    data_model: CampaignData,
    *,
    has_rows: dict[str, bool],
    extra: dict[str, list[dict[str, Any]]],
    force_partial: set[str],
) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    """按 B1 递归 null 治理推导 availability/limitations/data_status。"""
    availability: dict[str, Any] = {}
    limitations: list[dict[str, Any]] = []
    for section in _SECTION_ORDER:
        field = CampaignData.model_fields[section]
        nulls = list(
            iter_null_numeric_paths(
                field.annotation, getattr(data_model, section), section
            )
        )
        section_limitations = list(extra.get(section) or ())
        if not has_rows.get(section, False):
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
        for section in CampaignReportV2.REQUIRED_SECTIONS
    )
    return ("restricted" if restricted else "complete"), availability, limitations


# ---------------------------------------------------------------------------
# Gate C Task 4：对比/归属/自然传播/受众/内部指标/ROI 章节
# ---------------------------------------------------------------------------


def _group_totals(rows: list[RowRef]) -> dict[str, int | None]:
    """按行聚合 volume/engagement/posts/creators 四指标（社媒口径）。

    observed 显式跟踪（Gate C 第三轮）：字段至少出现一次 → 真实合计（含 0）；
    字段完全没出现 → None，绝不把缺失当 0，也绝不把真实 0 当缺失。
    """
    totals = {"volume": 0, "engagement": 0, "posts": 0}
    observed = {"volume": False, "engagement": False}
    creator_ids: set[str] = set()
    for ref in rows:
        row = ref.row
        volume = whole(first(row, VOLUME_KEYS))
        engagement = whole(first(row, ENGAGEMENT_KEYS))
        if volume is not None:
            totals["volume"] += volume
            observed["volume"] = True
        if engagement is not None:
            totals["engagement"] += engagement
            observed["engagement"] = True
        totals["posts"] += 1
        author_id = _row_author_id(row)
        if author_id is not None:
            creator_ids.add(author_id)
    return {
        "volume": totals["volume"] if observed["volume"] else None,
        "engagement": totals["engagement"] if observed["engagement"] else None,
        "posts": totals["posts"],
        "creators": len(creator_ids) if creator_ids else None,
    }


def _rate_change(current: float | None, baseline: float | None) -> float | None:
    if current is None or baseline is None or baseline == 0:
        return None
    return round((current - baseline) / baseline, 4)


def _comparison_series(
    metric: str, current: int | None, baseline: int | None
) -> dict[str, Any]:
    return {
        "metric": metric,
        "current": current,
        "baseline": baseline,
        "delta": round(current - baseline, 2) if current is not None and baseline is not None else None,
        "rate": _rate_change(current, baseline),
    }


def _build_comparisons(
    current_rows: list[RowRef],
    baseline_rows: list[RowRef],
    post_rows: list[RowRef],
    collector: LineageCollector,
) -> tuple[dict[str, Any], bool]:
    """活动期 vs 活动前（current_baseline）/ 活动后观察期（current_post）。

    current 分组缺省时回退 posts/social（DataTap 口径）；对比只在对应分组有
    行时生成，绝不估算。

    lineage 归期（P1-2）：current 只引用 current_rows；baseline 按系列引用
    baseline_rows（current_baseline）或 post_rows（current_post，该字段装的是
    post 期值）；delta/rate 同时引用 current_rows 与对应对比期行集合，按
    (evidence_id, source_path) 去重保序；真实 0 登记、None 不登记，三组
    Evidence 绝不混淆。
    """
    current_totals = _group_totals(current_rows)
    baseline_totals = _group_totals(baseline_rows)
    post_totals = _group_totals(post_rows)
    has_rows = bool(current_rows or baseline_rows or post_rows)
    metrics = ("volume", "engagement", "posts", "creators")

    current_baseline = [
        _comparison_series(metric, current_totals[metric], baseline_totals[metric])
        for metric in metrics
    ]
    current_post = [
        _comparison_series(metric, current_totals[metric], post_totals[metric])
        for metric in metrics
    ]
    # lineage 只登记 payload 真实叶子（current/baseline/delta/rate）；空系列不登记。
    for series_name, series, comparison_rows in (
        ("current_baseline", current_baseline, baseline_rows),
        ("current_post", current_post, post_rows),
    ):
        if not comparison_rows:
            continue
        for index, entry in enumerate(series):
            for field in ("current", "baseline", "delta", "rate"):
                value = entry[field]
                if value is None:
                    continue
                if field == "current":
                    sources = current_rows
                elif field == "baseline":
                    sources = comparison_rows
                else:
                    sources = _dedup_rows([*current_rows, *comparison_rows])
                collector.add(
                    f"/data/comparisons/{series_name}/{index}/{field}",
                    sources,
                )
    return {
        "current_baseline": current_baseline if baseline_rows else [],
        "current_post": current_post if post_rows else [],
    }, has_rows


# 归属语义标准化 token（字段语义，绝不用宽泛子串包含）。
_PAID_TOKENS = ("付费", "商单", "广告", "投放", "paid", "ad", "commercial", "sponsored", "推广")
_ORGANIC_TOKENS = (
    "自然",
    "免费",
    "自来水",
    "organic",
    "unpaid",
    "非付费",
    "非商单",
    "非广告",
    "非投放",
)
# 否定前缀：紧邻付费 token 之前时整体表达自然语义（「非付费」不得命中付费）。
_NEGATION_PREFIXES = ("非", "不", "无", "未", "non", "un", "not", "in")


def _contains_token(folded: str, token: str) -> bool:
    """ASCII token 用词边界匹配（避免 ad 命中 upload/head 等），中文 token 直接包含。"""
    if token.isascii():
        return re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", folded) is not None
    return token in folded


def _attribution_kind(ref: RowRef) -> tuple[str, str | None]:
    """归属分类；返回 ``(kind, 命中的归属字段名)``。

    按 ATTRIBUTION_KEYS 顺序取第一个有非空值的字段（Gate C 第三轮保留
    命中的字段名）：
    1. 「是否付费」布尔字段：是/true/1/yes → paid_confirmed；否/false/0/no
       → organic；其余值 → unknown（不得默认付费）；
    2. 文本字段（归属/投放类型/付费自然/attribution）：自然 token（含
       非付费/非商单/unpaid）优先 → organic；否定前缀+付费 token → organic；
       付费 token → paid_confirmed；其余 → unknown。
    无归属字段或字段值为空 → ``(unknown, None)``。
    """
    for key in ATTRIBUTION_KEYS:
        if key not in ref.row:
            continue
        raw = ref.row[key]
        if raw is None:
            continue
        folded = str(raw).strip().casefold()
        if not folded:
            continue
        if key in _BOOLEAN_ATTRIBUTION_KEYS:
            if folded in _PAID_BOOL_VALUES:
                return "paid_confirmed", key
            if folded in _ORGANIC_BOOL_VALUES:
                return "organic", key
            return "unknown", key
        if any(_contains_token(folded, token) for token in _ORGANIC_TOKENS):
            return "organic", key
        for prefix in _NEGATION_PREFIXES:
            if folded.startswith(prefix):
                rest = folded[len(prefix) :].strip("-–· ")
                if rest and any(_contains_token(rest, token) for token in _PAID_TOKENS):
                    return "organic", key
        if any(_contains_token(folded, token) for token in _PAID_TOKENS):
            return "paid_confirmed", key
        return "unknown", key
    return "unknown", None


def _build_attribution(
    post_rows: list[RowRef], collector: LineageCollector
) -> tuple[dict[str, Any], bool]:
    """内容归属：paid_confirmed/organic/unknown。

    无归属字段的帖计入 unknown（没有证据不得自动认定付费投放）。
    """
    counts = {"paid_confirmed": 0, "organic": 0, "unknown": 0}
    for ref in post_rows:
        counts[_attribution_kind(ref)[0]] += 1
    total = sum(counts.values())
    if post_rows:
        for field in ("paid_confirmed", "organic", "unknown"):
            collector.add(f"/data/attribution/{field}", post_rows)
        collector.add("/data/attribution/paid_confirmed_share", post_rows)
    return {
        "paid_confirmed": counts["paid_confirmed"] if post_rows else None,
        "organic": counts["organic"] if post_rows else None,
        "unknown": counts["unknown"] if post_rows else None,
        "paid_confirmed_share": round(counts["paid_confirmed"] / total, 4) if post_rows and total else None,
    }, bool(post_rows)


def _build_organic_summary(
    post_rows: list[RowRef], collector: LineageCollector
) -> tuple[dict[str, Any], bool]:
    """organic_summary 只聚合确认归为 organic 的行（Gate C 审核）。

    observed 规则（Gate C 第三轮）：声量/互动字段出现且合计为 0 → 保留 0 并
    登记 lineage；字段完全没出现 → None，绝不用 truthiness 判断。
    """
    organic_rows = [ref for ref in post_rows if _attribution_kind(ref)[0] == "organic"]
    if not organic_rows:
        return {}, False
    volume = _sum_number(organic_rows, VOLUME_KEYS)
    engagement = _sum_number(organic_rows, ENGAGEMENT_KEYS)
    if volume is not None:
        collector.add("/data/organic_summary/volume", organic_rows)
    if engagement is not None:
        collector.add("/data/organic_summary/engagement", organic_rows)
    collector.add("/data/organic_summary/posts", organic_rows)
    return {
        "volume": volume,
        "engagement": engagement,
        "posts": len(organic_rows),
        "share_of_volume": None,  # 无自然/总声量对比数据时不估算
    }, True


def _build_audience_regions(
    post_rows: list[RowRef], collector: LineageCollector
) -> tuple[list[dict[str, Any]], bool]:
    """按发帖用户地区聚合（REGION_KEYS）；无地区字段返回空。

    observed 规则（Gate C 第三轮）：地域声量字段出现 → 真实合计（含 0）；
    完全没出现 → None。地域总声量为 0 时 share=None，绝不伪造 share=0。
    """
    buckets: dict[str, dict[str, Any]] = {}
    for ref in post_rows:
        region = text(first(ref.row, REGION_KEYS))
        if region is None:
            continue
        bucket = buckets.setdefault(region, {"volume": 0, "observed": False})
        volume = whole(first(ref.row, VOLUME_KEYS))
        if volume is not None:
            bucket["volume"] += volume
            bucket["observed"] = True
        collector.add("/data/audience_regions", [ref])
    if not buckets:
        return [], False
    total_volume = sum(bucket["volume"] for bucket in buckets.values())
    total_observed = any(bucket["observed"] for bucket in buckets.values())
    regions: list[dict[str, Any]] = []
    for region, bucket in buckets.items():
        index = len(regions)
        volume = bucket["volume"] if bucket["observed"] else None
        share = (
            round(bucket["volume"] / total_volume, 4)
            if total_observed and total_volume > 0
            else None
        )
        regions.append({"region": region, "volume": volume, "share": share})
        for field, value in (("volume", volume), ("share", share)):
            if value is not None:
                collector.add(f"/data/audience_regions/{index}/{field}", post_rows)
    return regions, True


def _sum_number(rows: list[RowRef], keys: tuple[str, ...]) -> int | None:
    """对行集合的数值求和；全部缺失返回 None。"""
    total: int | None = None
    for ref in rows:
        value = whole(first(ref.row, keys))
        if value is not None:
            total = (total or 0) + value
    return total


def _build_internal_metrics(
    upload_rows: list[RowRef], collector: LineageCollector
) -> tuple[dict[str, Any], bool]:
    """成本/转化以 upload 为准；无 upload 时不估算（Gate C 审核）。

    存在明确「合计」行时优先采用合计行，否则聚合平台/KOL 明细行；绝不同时
    累加合计行和明细行造成重复。
    """
    if not upload_rows:
        return {}, False
    total_rows = [
        ref
        for ref in upload_rows
        if "合计" in (text(first(ref.row, PLATFORM_KEYS)) or "")
    ]
    source_rows = total_rows if total_rows else upload_rows
    spend = _sum_number(source_rows, SPEND_KEYS)
    impressions = _sum_number(source_rows, IMPRESSION_KEYS)
    conversions = _sum_number(source_rows, CONVERSION_KEYS)
    revenue = _sum_number(source_rows, REVENUE_KEYS)
    if spend is not None:
        collector.add("/data/internal_metrics/spend", source_rows)
    if impressions is not None:
        collector.add("/data/internal_metrics/impressions", source_rows)
    if conversions is not None:
        collector.add("/data/internal_metrics/conversions", source_rows)
    if revenue is not None:
        collector.add("/data/internal_metrics/revenue", source_rows)
    cpc = round(spend / conversions, 2) if spend is not None and conversions else None
    cpm = (
        round(spend / impressions * 1000, 2)
        if spend is not None and impressions
        else None
    )
    if cpc is not None:
        collector.add("/data/internal_metrics/cpc", source_rows)
    if cpm is not None:
        collector.add("/data/internal_metrics/cpm", source_rows)
    return {
        "spend": spend,
        "impressions": impressions,
        "conversions": conversions,
        "revenue": revenue,
        "cpc": cpc,
        "cpm": cpm,
    }, any(v is not None for v in (spend, impressions, conversions, revenue))


def _build_roi(
    scope: dict[str, Any], internal_metrics: dict[str, Any]
) -> dict[str, Any] | None:
    """ROI 门禁（Gate C 审核）：spend>0 + 明确归因口径 + 曝光 + 收入齐全才生成；
    comparison_mode 只是周期比较方式，绝不能作为归因窗口；任一条件不满足 → None；
    spend=0 不除零。

    Gate C 复审：ROI/ROAS 与 CPC 分别处理。ROI/ROAS 只依赖 spend+revenue+归因窗口，
    与转化数无关；``conversions=0`` 是有效数据（不阻断 ROI/ROAS），只有转化字段
    完全缺失（None）才视为数据不可用。CPC 需要 ``conversions>0`` 作分母，在
    internal_metrics 侧单独处理（conversions=0 → CPC=None）。
    """
    spend = internal_metrics.get("spend")
    impressions = internal_metrics.get("impressions")
    conversions = internal_metrics.get("conversions")
    revenue = internal_metrics.get("revenue")
    attribution_rules = [
        str(item)
        for item in scope.get("attribution_rules") or ()
        if isinstance(item, str) and item.strip()
    ]
    if (
        spend is None
        or spend <= 0
        or not attribution_rules
        or impressions is None
        or impressions <= 0
        or conversions is None
        or revenue is None
    ):
        return None
    window = "、".join(attribution_rules)
    # spend > 0 已显式校验，分母安全。
    roi = round((revenue - spend) / spend, 4)
    roas = round(revenue / spend, 4)
    return {
        "spend": spend,
        "revenue": revenue,
        "conversions": conversions,
        "attribution_window": window,
        "roi": roi,
        "roas": roas,
    }


# ---------------------------------------------------------------------------
# 公开入口
# ---------------------------------------------------------------------------


def build_campaign_report_draft(
    *,
    scope: dict[str, Any],
    evidence: dict[str, list[tuple[str, Any]]],
    narrative: dict[str, Any] | None = None,
    top_posts_limit: int = 20,
    data_as_of: Any = None,
    source_names: tuple[str, ...] = ("campaign_evidence",),
) -> DraftBuildResult:
    """把模型选定的 Evidence 转换为 ``campaign_report_v2`` Draft。

    ``evidence`` 分组：``posts``（原始帖行，必需章节的主要数据源）与
    ``sentiment``（可选情感明细行；缺失时用 posts 行情感字段）。posts 缺失时
    产出 restricted 产物并披露 limitation；全部分组都提取不到行时抛
    :class:`DraftBuildError`（由工具层结构化回喂模型）。
    """
    try:
        scope_model = CampaignScope.model_validate(scope)
    except ValidationError as exc:
        raise DraftBuildError(f"invalid campaign scope: {exc}") from exc

    groups = {group: _extract_group(evidence.get(group)) for group in EVIDENCE_GROUPS}
    if not any(groups.values()):
        raise DraftBuildError(
            "build_campaign_report_draft requires at least one usable evidence row"
        )

    collector = LineageCollector()
    post_rows = _dedup_rows(_group_posts(groups[GROUP_POSTS]))
    # Gate C Task 4：社媒指标以 DataTap 为主（post/social/posts 分组合并去重），
    # 成本/转化以 upload 为主；冲突值双保留并生成 limitation。
    # Gate C 复审：合并行列表按 (evidence_id, source_path) 去重，同一 Evidence
    # 行被多分组引用时只计一次。
    social_rows = _dedup_rows(_group_posts([*groups[GROUP_POSTS], *groups[GROUP_SOCIAL]]))
    upload_rows = _dedup_rows(groups[GROUP_UPLOAD])
    current_rows = _dedup_rows(_group_posts([*groups[GROUP_CURRENT], *groups[GROUP_POSTS]]))
    baseline_rows = _dedup_rows(_group_posts(groups[GROUP_BASELINE]))
    post_period_rows = _dedup_rows(_group_posts(groups[GROUP_POST]))

    # 社媒冲突检测：upload 行若同时携带声量/互动且与 DataTap 不同，双值保留。
    # observed 规则（Gate C 第三轮）：DataTap 侧字段出现合计 0（观测值）vs
    # upload 非 0 是真实冲突；DataTap 字段完全没出现（未观测）不参与比较。
    social_conflicts: list[dict[str, Any]] = []
    upload_volume = _sum_number(upload_rows, VOLUME_KEYS)
    upload_engagement = _sum_number(upload_rows, ENGAGEMENT_KEYS)
    datatap_volume = _sum_number(social_rows, VOLUME_KEYS)
    datatap_engagement = _sum_number(social_rows, ENGAGEMENT_KEYS)
    if upload_volume is not None and datatap_volume is not None and upload_volume != datatap_volume:
        social_conflicts.append(
            {
                "code": "social_metric_conflict",
                "message": (
                    f"声量冲突双值保留：DataTap {datatap_volume} / 用户资料 "
                    f"{upload_volume}，未静默覆盖"
                ),
                "affected_paths": ["overview.total_volume", "internal_metrics.spend"],
            }
        )
    if upload_engagement is not None and datatap_engagement is not None and upload_engagement != datatap_engagement:
        social_conflicts.append(
            {
                "code": "social_metric_conflict",
                "message": (
                    f"互动数冲突双值保留：DataTap {datatap_engagement} / 用户资料 "
                    f"{upload_engagement}，未静默覆盖"
                ),
                "affected_paths": ["overview.total_engagement", "internal_metrics.spend"],
            }
        )

    # ---- sentiment：明细分组优先，缺失时回退 posts 行情感字段（每帖计 1） ----
    sentiment_source = groups[GROUP_SENTIMENT]
    if not sentiment_source:
        sentiment_source = [
            ref for ref in post_rows if first(ref.row, SENTIMENT_KEYS) is not None
        ]
    sentiment, sentiment_has_rows = build_sentiment_section(sentiment_source, collector)
    score_rows = [
        ref for ref in sentiment_source if first(ref.row, SENTIMENT_KEYS) is not None
    ]
    summary_counts = sentiment["summary"]
    overall_score = (
        sentiment_score(
            summary_counts["positive"]["count"],
            summary_counts["neutral"]["count"],
            summary_counts["negative"]["count"],
        )
        if sentiment_has_rows
        else None
    )

    overview, overview_has_rows = _build_overview(
        post_rows, overall_score, score_rows, collector
    )
    platform_contributions, contributions_has_rows = _build_platform_contributions(
        post_rows, collector
    )
    timeline, timeline_has_rows = _build_timeline(post_rows, collector)
    kol_contributions, kols_has_rows = _build_kol_contributions(post_rows, collector)
    content_types, content_has_rows = _build_content_types(post_rows, collector)
    top_posts, posts_meta = build_top_posts(
        post_rows, collector, limit=min(max(top_posts_limit, 1), 20)
    )

    # Gate C Task 4 章节。
    comparisons, comparisons_has_rows = _build_comparisons(
        current_rows, baseline_rows, post_period_rows, collector
    )
    attribution, attribution_has_rows = _build_attribution(social_rows, collector)
    organic_summary, organic_summary_has_rows = _build_organic_summary(
        social_rows, collector
    )
    audience_regions, audience_regions_has_rows = _build_audience_regions(
        social_rows, collector
    )
    internal_metrics, internal_metrics_has_rows = _build_internal_metrics(
        upload_rows, collector
    )
    internal_metrics = internal_metrics or None
    roi = _build_roi(scope_model.model_dump(), internal_metrics or {})
    if roi is not None:
        for field in ("spend", "revenue", "conversions", "roi", "roas"):
            if roi[field] is not None:
                collector.add(f"/data/roi/{field}", upload_rows)

    force_partial: set[str] = set()
    extra_limitations: dict[str, list[dict[str, Any]]] = {}
    if posts_meta["skipped"]:
        force_partial.add("top_posts")
        extra_limitations.setdefault("top_posts", []).append(
            {
                "code": "post_row_incomplete",
                "message": "部分热帖缺少帖子 ID 或发布时间，已跳过",
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

    data = {
        "overview": overview,
        "platform_contributions": platform_contributions,
        "timeline": timeline,
        "kol_contributions": kol_contributions,
        "content_types": content_types,
        "sentiment": sentiment,
        "top_posts": top_posts,
        "comparisons": comparisons,
        "attribution": attribution,
        "organic_summary": organic_summary,
        "audience_regions": audience_regions,
        "internal_metrics": internal_metrics,
        "roi": roi,
    }
    try:
        data_model = CampaignData.model_validate(data)
    except ValidationError as exc:
        raise DraftBuildError(f"invalid campaign data: {exc}") from exc

    has_rows = {
        "overview": overview_has_rows,
        "platform_contributions": contributions_has_rows,
        "timeline": timeline_has_rows,
        "kol_contributions": kols_has_rows,
        "content_types": content_has_rows,
        "sentiment": sentiment_has_rows,
        "top_posts": bool(top_posts),
        "comparisons": comparisons_has_rows,
        "attribution": attribution_has_rows,
        "organic_summary": organic_summary_has_rows,
        "audience_regions": audience_regions_has_rows,
        "internal_metrics": internal_metrics_has_rows,
        "roi": roi is not None,
    }
    data_status, availability, limitations = _assemble_availability(
        data_model,
        has_rows=has_rows,
        extra=extra_limitations,
        force_partial=force_partial,
    )
    for conflict in social_conflicts:
        limitations.append(conflict)
        force_partial.add("overview")
        availability["overview"]["status"] = "partial"
        if "social_metric_conflict" not in availability["overview"]["reason_codes"]:
            availability["overview"]["reason_codes"].append("social_metric_conflict")
        data_status = "restricted"

    payload = {
        "schema_version": SCHEMA_VERSION,
        "module": "campaign",
        "data_status": data_status,
        "availability": availability,
        "limitations": limitations,
        "methodology": methodology_dict(data_as_of=data_as_of, source_names=source_names),
        "scope": scope_model.model_dump(),
        "data": data,
        "narrative": narrative
        if narrative is not None
        else _default_narrative(scope_model.brand, scope_model.campaign, data_status),
    }
    try:
        CampaignReportV2.model_validate(payload)  # fail-fast：builder 输出必须合法。
    except ValidationError as exc:
        raise DraftBuildError(f"invalid campaign_report_v2 payload: {exc}") from exc

    refs = collector.build()
    missing = required_numeric_pointers(payload) - {ref["artifact_path"] for ref in refs}
    if missing:
        raise DraftBuildError(
            "campaign builder lineage coverage incomplete: " + ", ".join(sorted(missing))
        )

    return DraftBuildResult(
        module="campaign",
        schema_version=SCHEMA_VERSION,
        artifact_type=SCHEMA_VERSION,
        business_fields={"brand": scope_model.brand, "campaign": scope_model.campaign},
        payload=payload,
        evidence_refs=refs,
    )


__all__ = [
    "EVIDENCE_GROUPS",
    "SCHEMA_VERSION",
    "build_campaign_report_draft",
]
