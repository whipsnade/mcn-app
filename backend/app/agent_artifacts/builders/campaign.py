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
    SENTIMENT_KEYS,
    SHARE_KEYS,
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

# Evidence 分组键（工具入参 evidence 的合法键）。
GROUP_POSTS = "posts"
GROUP_SENTIMENT = "sentiment"

EVIDENCE_GROUPS = (GROUP_POSTS, GROUP_SENTIMENT)

_SECTION_ORDER = (
    "overview",
    "platform_contributions",
    "timeline",
    "kol_contributions",
    "content_types",
    "sentiment",
    "top_posts",
)

_KOL_LIMIT = 20


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
    post_rows = _group_posts(groups[GROUP_POSTS])

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
    }
    data_status, availability, limitations = _assemble_availability(
        data_model,
        has_rows=has_rows,
        extra=extra_limitations,
        force_partial=force_partial,
    )

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
