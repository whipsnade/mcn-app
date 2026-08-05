"""``kol_analysis_v2`` Draft builder（设计 §12.1 / Task 16）。

对不可变 ``kol_selection_v3`` Version 做组合分析：
- 通过 ``parent_artifact_version_id`` 固定到当时分析的名单 Version（不可变）；
- 复用稳定身份 ``kol-analysis:{selection_artifact_id}``；
- 从名单数据计算 5 个分布 + kol_trend/top_kols（≤20）；
- narrative 通过 ``supporting_paths`` 引用 ``data``；
- lineage 引用名单 Version 的字段，经 Task 11 递归追溯到名单 Evidence。

builder 只做确定性转换，不调用 MCP。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from pydantic import ValidationError

from app.agent_artifacts.builders.common import (
    DraftBuildError,
    DraftBuildResult,
    distribution,
    methodology_dict,
)
from app.agent_artifacts.payloads.kol_analysis import KolAnalysisV2

SCHEMA_VERSION = "kol_analysis_v2"

# 名单最多 20 项（跨平台 Top20），分析侧 kol_trend/top_kols 上限一致。
_MAX_ITEMS = 20

_SUMMARY_FIELDS = (
    "kol_count",
    "total_followers",
    "total_active_followers",
    "total_engagement",
    "avg_score",
)

# §6.3 递归 null 治理：kol_trend/top_kols 数组元素内的 Optional 数值叶子同样
# 受治理——出现 null 时对应章节必须 partial + limitation，不得谎称 complete。
_TREND_FIELDS = (
    "followers",
    "active_followers",
    "engagement_total",
    "avg_engagement",
    "growth_rate",
    "score",
)
_TOP_KOL_FIELDS = ("score", "engagement_total")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _bucket_label(value: Any, *, follower: bool) -> str | None:
    """把数值映射到展示分桶；非数字/None 返回 None（不进分布）。"""
    if not _is_number(value):
        return None
    if follower:
        if value < 100_000:
            return "10万以下"
        if value < 500_000:
            return "10万-50万"
        if value < 1_000_000:
            return "50万-100万"
        return "100万以上"
    if value < 10_000:
        return "1万以下"
    if value < 50_000:
        return "1万-5万"
    if value < 200_000:
        return "5万-20万"
    return "20万以上"


def _sum_field(items: list[dict[str, Any]], field: str) -> int | float | None:
    values = [item.get(field) for item in items if _is_number(item.get(field))]
    if not values:
        return None
    total = sum(values)
    return int(total) if all(isinstance(value, int) for value in values) else total


def _analysis_data(items: list[dict[str, Any]]) -> dict[str, Any]:
    kol_count = len(items)
    scores = [
        item["score_snapshot"]["total"]
        for item in items
        if isinstance(item.get("score_snapshot"), dict)
        and _is_number(item["score_snapshot"].get("total"))
    ]
    return {
        "summary": {
            "kol_count": kol_count,
            "total_followers": _sum_field(items, "followers"),
            "total_active_followers": _sum_field(items, "active_followers"),
            "total_engagement": _sum_field(items, "engagement_total"),
            "avg_score": round(sum(scores) / len(scores), 2) if scores else None,
        },
        "platform_distribution": distribution([item.get("platform") for item in items]),
        "rating_distribution": distribution(
            [(item.get("score_snapshot") or {}).get("rating") for item in items]
        ),
        "follower_distribution": distribution(
            [_bucket_label(item.get("followers"), follower=True) for item in items]
        ),
        "engagement_distribution": distribution(
            [_bucket_label(item.get("engagement_total"), follower=False) for item in items]
        ),
        "region_distribution": _region_distribution(items, kol_count),
        "kol_trend": [
            {
                "platform": item.get("platform"),
                "kol_uid": item.get("kol_uid"),
                "nickname": item.get("nickname"),
                "followers": item.get("followers"),
                "active_followers": item.get("active_followers"),
                "engagement_total": item.get("engagement_total"),
                "avg_engagement": item.get("avg_engagement"),
                "growth_rate": item.get("growth_rate"),
                "score": (item.get("score_snapshot") or {}).get("total"),
            }
            for item in items[:_MAX_ITEMS]
        ],
        "top_kols": [
            {
                "rank": index + 1,
                "platform": item.get("platform"),
                "kol_uid": item.get("kol_uid"),
                "nickname": item.get("nickname"),
                "score": (item.get("score_snapshot") or {}).get("total"),
                "engagement_total": item.get("engagement_total"),
                "rating": (item.get("score_snapshot") or {}).get("rating"),
            }
            for index, item in enumerate(items[:_MAX_ITEMS])
        ],
    }


def _region_distribution(items: list[dict[str, Any]], kol_count: int) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for item in items:
        for region in (item.get("audience") or {}).get("regions") or ():
            if region:
                counts[region] = counts.get(region, 0) + 1
    if not counts:
        return []
    total = kol_count or 1
    return [
        {"key": region, "label": region, "count": count, "share": round(count / total, 4)}
        for region, count in sorted(counts.items())
    ]


def _analysis_narrative(items: list[dict[str, Any]], data: dict[str, Any]) -> dict[str, Any]:
    summary = data["summary"]
    top = data["top_kols"]
    platforms = data["platform_distribution"]
    ratings = data["rating_distribution"]

    portfolio_findings: list[dict[str, Any]] = []
    if top:
        portfolio_findings.append(
            {
                "title": "头部 KOL 集中",
                "detail": f"Top KOL {top[0]['nickname']} 综合评分最高。",
                "supporting_paths": ["data.top_kols.0.score"],
            }
        )
    mix_recommendations: list[dict[str, Any]] = []
    if platforms:
        mix_recommendations.append(
            {
                "title": "平台分布",
                "detail": "平台结构清晰，可据此规划投放组合。",
                "supporting_paths": ["data.platform_distribution.0.count"],
            }
        )
    risk_notes: list[dict[str, Any]] = []
    if ratings:
        risk_notes.append(
            {
                "title": "评级分布",
                "detail": "按评级分布评估名单整体质量。",
                "supporting_paths": ["data.rating_distribution.0.count"],
            }
        )
    return {
        "executive_summary": f"对 {summary['kol_count']} 位 KOL 的名单进行组合分析。",
        "portfolio_findings": portfolio_findings,
        "mix_recommendations": mix_recommendations,
        "risk_notes": risk_notes,
    }


def _assemble_payload(
    *,
    scope: dict[str, Any],
    data: dict[str, Any],
    narrative: dict[str, Any],
    data_status: str,
    limitations: list[dict[str, Any]],
    availability: dict[str, Any],
    data_as_of: datetime | None,
    source_names: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "module": "kol",
        "data_status": data_status,
        "availability": availability,
        "limitations": limitations,
        "methodology": methodology_dict(data_as_of=data_as_of, source_names=source_names),
        "scope": scope,
        "data": data,
        "narrative": narrative,
    }


def _analysis_lineage(
    payload: dict[str, Any],
    parent_version_id: str,
    covered: set[str],
    selection_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """名单 Version 字段 → Evidence 递归；只引用名单 builder 已提供 lineage 的路径。

    ``selection_items`` 与分析侧 kol_trend/top_kols 同序（名单 ≤20，跨平台 Top20）。
    分布分桶按名单项的评分/受众字段计算（分析 payload 的 kol_trend 不含这些字段）。
    """
    data = payload["data"]
    top_kols = data["top_kols"]
    count = len(selection_items)
    refs: list[dict[str, Any]] = []

    def add(path: str, selection_paths: list[str]) -> None:
        usable = [candidate for candidate in selection_paths if candidate in covered]
        if not usable:
            return
        refs.append(
            {
                "artifact_path": path,
                "sources": [
                    {
                        "source_type": "artifact",
                        "artifact_version_id": parent_version_id,
                        "source_path": candidate,
                    }
                    for candidate in usable
                ],
                "derivation": None,
            }
        )

    # summary 聚合：引用全部贡献项（同一顺序，名单项 ≤ 20）。
    add("/data/summary/kol_count", [f"/data/items/{i}/rank" for i in range(count)])
    add("/data/summary/total_followers", [f"/data/items/{i}/followers" for i in range(count)])
    add(
        "/data/summary/total_active_followers",
        [f"/data/items/{i}/active_followers" for i in range(count)],
    )
    add("/data/summary/total_engagement", [f"/data/items/{i}/engagement_total" for i in range(count)])
    add("/data/summary/avg_score", [f"/data/items/{i}/score_snapshot/total" for i in range(count)])

    # 分布 count/share：引用落入该桶的名单项的排序路径。
    def _bucket_indices(key_fn: Callable[[dict[str, Any]], Any]) -> dict[Any, list[int]]:
        buckets: dict[Any, list[int]] = {}
        for index, item in enumerate(selection_items):
            key = key_fn(item)
            if key is not None:
                buckets.setdefault(key, []).append(index)
        return buckets

    for collection, key_fn in (
        ("platform_distribution", lambda item: item.get("platform")),
        ("rating_distribution", lambda item: (item.get("score_snapshot") or {}).get("rating")),
        ("follower_distribution", lambda item: _bucket_label(item.get("followers"), follower=True)),
        (
            "engagement_distribution",
            lambda item: _bucket_label(item.get("engagement_total"), follower=False),
        ),
    ):
        bucket_indices = _bucket_indices(key_fn)
        for j, entry in enumerate(data[collection]):
            indices = bucket_indices.get(entry["key"], [])
            paths = [f"/data/items/{index}/rank" for index in indices]
            add(f"/data/{collection}/{j}/count", paths)
            add(f"/data/{collection}/{j}/share", paths)

    region_indices: dict[str, list[int]] = {}
    for index, item in enumerate(selection_items):
        for region in (item.get("audience") or {}).get("regions") or ():
            if region:
                region_indices.setdefault(region, []).append(index)
    for j, entry in enumerate(data["region_distribution"]):
        indices = region_indices.get(entry["key"], [])
        paths = [f"/data/items/{index}/rank" for index in indices]
        add(f"/data/region_distribution/{j}/count", paths)
        add(f"/data/region_distribution/{j}/share", paths)

    # kol_trend 单项字段。
    for i in range(count):
        add(f"/data/kol_trend/{i}/followers", [f"/data/items/{i}/followers"])
        add(f"/data/kol_trend/{i}/active_followers", [f"/data/items/{i}/active_followers"])
        add(f"/data/kol_trend/{i}/engagement_total", [f"/data/items/{i}/engagement_total"])
        add(f"/data/kol_trend/{i}/avg_engagement", [f"/data/items/{i}/avg_engagement"])
        add(f"/data/kol_trend/{i}/growth_rate", [f"/data/items/{i}/growth_rate"])
        add(f"/data/kol_trend/{i}/score", [f"/data/items/{i}/score_snapshot/total"])

    # top_kols 单项字段。
    for i in range(len(top_kols)):
        add(f"/data/top_kols/{i}/rank", [f"/data/items/{i}/rank"])
        add(f"/data/top_kols/{i}/score", [f"/data/items/{i}/score_snapshot/total"])
        add(f"/data/top_kols/{i}/engagement_total", [f"/data/items/{i}/engagement_total"])

    return refs


def build_kol_analysis_draft(
    *,
    selection_artifact_id: str,
    selection_payload: dict[str, Any],
    parent_artifact_version_id: str,
    selection_version: str,
    analysis_period: str | None = None,
    selection_refs: list[dict[str, Any]] | None = None,
    source_names: tuple[str, ...] = ("kol_evidence",),
    data_as_of: datetime | None = None,
    narrative: dict[str, Any] | None = None,
) -> DraftBuildResult:
    """对不可变名单 Version 构建 ``kol_analysis_v2`` Draft。

    ``parent_artifact_version_id`` 固定到当时分析的名单 Version；稳定身份复用
    ``kol-analysis:{selection_artifact_id}``。

    ``narrative``：模型提供的叙事（设计 §6.1；H4——Reviewer 可要求逐人分析，
    确定性组合级模板叙事无法满足），写入 payload 前经
    ``KolAnalysisV2.model_validate`` 强校验（含 supporting_paths 必须指向
    data 内真实路径）；缺省时按名单数据确定性生成兜底叙事。无候选的
    restricted 路径恒用 builder 自己的受限披露叙事（此时无 data 可引用，
    不采用模型叙事）。
    """
    scope = {
        "selection_artifact_id": selection_artifact_id,
        "selection_version": selection_version,
        "analysis_period": analysis_period,
    }
    data = selection_payload.get("data") or {}
    items = list(data.get("items") or [])

    def _result(payload: dict[str, Any], refs: list[dict[str, Any]]) -> DraftBuildResult:
        return DraftBuildResult(
            module="kol-analysis",
            schema_version=SCHEMA_VERSION,
            artifact_type=SCHEMA_VERSION,
            business_fields={"selection_artifact_id": selection_artifact_id},
            payload=payload,
            evidence_refs=refs,
            parent_artifact_id=selection_artifact_id,
            parent_artifact_version_id=parent_artifact_version_id,
        )

    if not items:
        payload = _assemble_payload(
            scope=scope,
            data={
                "summary": {field: None for field in _SUMMARY_FIELDS},
                "platform_distribution": [],
                "rating_distribution": [],
                "follower_distribution": [],
                "engagement_distribution": [],
                "region_distribution": [],
                "kol_trend": [],
                "top_kols": [],
            },
            narrative={
                "executive_summary": "名单数据不足，无法完成 KOL 分析。",
                "portfolio_findings": [],
                "mix_recommendations": [],
                "risk_notes": [],
            },
            data_status="restricted",
            limitations=[
                {
                    "code": "insufficient_kol_data",
                    "message": "所选名单数据不足，KOL 分析受限披露",
                    "affected_paths": [f"summary.{field}" for field in _SUMMARY_FIELDS],
                }
            ],
            availability={
                "summary": {"status": "unavailable", "reason_codes": ["insufficient_kol_data"]},
                "kol_trend": {"status": "unavailable", "reason_codes": ["insufficient_kol_data"]},
                "top_kols": {"status": "unavailable", "reason_codes": ["insufficient_kol_data"]},
            },
            data_as_of=data_as_of,
            source_names=source_names,
        )
        try:
            KolAnalysisV2.model_validate(payload)
        except ValidationError as exc:
            raise DraftBuildError(f"invalid kol_analysis_v2 payload: {exc}") from exc
        return _result(payload, [])

    if not selection_refs:
        # 分析数据必须追溯到名单 Version 的 Evidence；缺少其 evidence_refs 时
        # lineage 会被静默丢弃（只能由 Reviewer 兜底）——builder 直接 fail-fast。
        raise DraftBuildError(
            "build_kol_analysis_draft requires selection_refs (the analyzed "
            "kol_selection_v3 version's evidence_refs) to emit lineage"
        )
    covered = {ref.get("artifact_path") for ref in selection_refs}

    analysis_data = _analysis_data(items)
    resolved_narrative = (
        narrative if narrative is not None else _analysis_narrative(items, analysis_data)
    )

    # 分章节收集 null Optional 数值叶子；任一章节有 null 即 partial + limitation。
    section_nulls: dict[str, list[str]] = {
        "summary": [
            f"summary.{field}"
            for field in _SUMMARY_FIELDS
            if analysis_data["summary"][field] is None
        ],
        "kol_trend": [
            f"kol_trend.{index}.{field}"
            for index, row in enumerate(analysis_data["kol_trend"])
            for field in _TREND_FIELDS
            if row.get(field) is None
        ],
        "top_kols": [
            f"top_kols.{index}.{field}"
            for index, row in enumerate(analysis_data["top_kols"])
            for field in _TOP_KOL_FIELDS
            if row.get(field) is None
        ],
    }
    restricted = any(section_nulls.values())
    availability = {
        section: {
            "status": "partial" if nulls else "complete",
            "reason_codes": ["insufficient_kol_data"] if nulls else [],
        }
        for section, nulls in section_nulls.items()
    }
    limitations = [
        {
            "code": "insufficient_kol_data",
            "message": "名单部分数值缺失，KOL 分析受限披露",
            "affected_paths": nulls,
        }
        for nulls in section_nulls.values()
        if nulls
    ]
    payload = _assemble_payload(
        scope=scope,
        data=analysis_data,
        narrative=resolved_narrative,
        data_status="restricted" if restricted else "complete",
        limitations=limitations,
        availability=availability,
        data_as_of=data_as_of,
        source_names=source_names,
    )
    try:
        KolAnalysisV2.model_validate(payload)
    except ValidationError as exc:
        raise DraftBuildError(f"invalid kol_analysis_v2 payload: {exc}") from exc

    refs = _analysis_lineage(payload, parent_artifact_version_id, covered, items)
    return _result(payload, refs)


__all__ = [
    "SCHEMA_VERSION",
    "build_kol_analysis_draft",
]
