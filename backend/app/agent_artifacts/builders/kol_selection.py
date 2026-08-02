"""``kol_selection_v3`` Draft builder（设计 §12.1 / Task 16）。

把模型已选定的 Evidence（KOL 列表数据）+ 查询范围转换为强类型
``kol_selection_v3`` Draft：评分委托给确定性 ``rank_kols`` 计算工具（Task 9），
它严格复用 ``selection.scoring_v2`` 的八维 ``kol_score_v2``。

关键不变量：
- ``score_snapshot`` 冻结 version/total/rating/stars/data_completeness 与全部
  八个维度，每项 ``{raw_score, weight, weighted_score, source, missing_reason}``；
- 缺失/无效维度记 0 分且不重分配权重；``growth_rate``/``quoted_price`` 只展示；
- 默认跨平台 Top20，按 ``engagement_total`` 降序；
- 数据不足时产出 ``restricted`` 产物（§12.1：数据不足必须 restricted）；
- 每个维度的原始输入引用 Evidence，派生评分（raw_score/weighted_score/total/
  rating/stars/data_completeness）引用已 settled 的 ``rank_kols`` 调用。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_artifacts.builders.common import (
    DraftBuildError,
    DraftBuildResult,
    distribution,
    methodology_dict,
)
from app.agent_artifacts.payloads.kol_selection import (
    SCORE_DIMENSIONS,
    WEIGHTS,
    KolSelectionScope,
    KolSelectionV3,
)
from app.agent_runtime.models import AgentToolCall
from app.agent_runtime.tools.calculation import RankKolsArgs, RankKolsTool
from app.agent_runtime.tools.contracts import ToolContext
from app.agent_runtime.tools.mcp import arguments_hash, logical_call_id_for
from app.selection.scoring_v2 import SCORE_VERSION_V2

SCHEMA_VERSION = "kol_selection_v3"
DEFAULT_LIMIT = 20

# 每个评分维度在 Evidence 项 ``score_inputs`` 里的原始输入键（spec §12.1）。
DIM_RAW_INPUT = {
    "industry_interest": "audience_interests",
    "target_region": "audience_regions",
    "target_age": "audience_age",
    "engagement": "average_interactions",
    "active_follower": "effective_follower_rate",
    "content": "content_score",
    "followers": "followers",
    "engagement_follower_ratio": "interaction_follower_ratio",
}

# 展示/排序用的数字字段：从 Evidence 原样复制，不进入 v2 总分。
_DISPLAY_NUMERIC_FIELDS = (
    "followers",
    "active_followers",
    "active_follower_rate",
    "growth_rate",
    "engagement_total",
    "avg_engagement",
    "likes",
    "comments",
    "shares",
    "quoted_price",
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _scoring_block() -> dict[str, Any]:
    return {
        "version": SCORE_VERSION_V2,
        "method": "weighted_sum",
        "weights": dict(WEIGHTS),
        "missing_value_policy": "missing_as_zero",
    }


def _rank_context(scope: dict[str, Any]) -> dict[str, Any]:
    """rank_kols 评分上下文：industry 取 category，region/age 取 audience 过滤。"""
    audience = scope.get("audience") or {}
    return {
        "industry": scope.get("category"),
        "regions": list(audience.get("regions") or ()),
        "age_ranges": list(audience.get("age_ranges") or ()),
    }


def _rank_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 Evidence 项投影为 rank_kols 消费的形状。

    只有 ``score_inputs`` 进入评分；顶层 followers/engagement_total/growth_rate/
    quoted_price 只用于展示与排序，绝不进入 v2 总分（spec 口径）。
    """
    projected: list[dict[str, Any]] = []
    for item in items:
        projected.append(
            {
                "platform": item.get("platform"),
                "kol_uid": item.get("kol_uid"),
                "nickname": item.get("nickname"),
                "followers": item.get("followers"),
                "engagement_total": item.get("engagement_total"),
                "growth_rate": item.get("growth_rate"),
                "quoted_price": item.get("quoted_price"),
                "score_inputs": item.get("score_inputs") or {},
            }
        )
    return projected


def _build_item(entry: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    """合并 rank_kols 排序结果与 Evidence 展示字段为单个名单项。"""
    snapshot = entry["score_snapshot"]
    audience = raw.get("audience") or {}
    return {
        "rank": entry["rank"],
        "platform": entry.get("platform") or raw.get("platform"),
        "kol_uid": entry.get("kol_uid") or raw.get("kol_uid"),
        "nickname": entry.get("nickname") or raw.get("kol_uid") or "",
        "avatar_url": raw.get("avatar_url"),
        "homepage_url": raw.get("homepage_url"),
        "followers": entry.get("followers"),
        "active_followers": raw.get("active_followers"),
        "active_follower_rate": raw.get("active_follower_rate"),
        "growth_rate": entry.get("growth_rate"),
        "engagement_total": entry.get("engagement_total"),
        "avg_engagement": raw.get("avg_engagement"),
        "likes": raw.get("likes"),
        "comments": raw.get("comments"),
        "shares": raw.get("shares"),
        "quoted_price": entry.get("quoted_price"),
        "reasons": list(raw.get("reasons") or ()),
        "missing_fields": list(entry.get("missing_fields") or ()),
        "audience": {
            "regions": list(audience.get("regions") or ()),
            "age_ranges": list(audience.get("age_ranges") or ()),
            "interests": list(audience.get("interests") or ()),
        },
        "score_snapshot": {
            "version": SCORE_VERSION_V2,
            "total": snapshot["total"],
            "rating": snapshot["rating"],
            "stars": snapshot["stars"],
            "data_completeness": snapshot["data_completeness"],
            "dimensions": {
                dim: {
                    "raw_score": dim_data["raw_score"],
                    "weight": dim_data["weight"],
                    "weighted_score": dim_data["weighted_score"],
                    "source": dim_data.get("source"),
                    "missing_reason": dim_data.get("missing_reason"),
                }
                for dim, dim_data in snapshot["dimensions"].items()
            },
        },
    }


def _narrative(items: list[dict[str, Any]], candidate_count: int) -> dict[str, Any]:
    selected_count = len(items)
    fit_findings = [
        {
            "text": f"第 {item['rank']} 名 {item['nickname']} 综合评分 "
            f"{item['score_snapshot']['total']}（{item['score_snapshot']['rating']}）",
            "kol_uid": item["kol_uid"],
            "supporting_paths": [f"data.items.{index}.score_snapshot.total"],
        }
        for index, item in enumerate(items[:3])
    ]
    risk_notes = [
        {
            "text": f"{item['nickname']} 数据缺失：{'、'.join(item['missing_fields'])}",
            "kol_uid": item["kol_uid"],
            "supporting_paths": [f"data.items.{index}.missing_fields"],
        }
        for index, item in enumerate(items)
        if item["missing_fields"]
    ]
    usage_advice = (
        [
            {
                "text": "优先合作评分与互动量双高的头部达人。",
                "supporting_paths": ["data.items.0.score_snapshot.total"],
            }
        ]
        if items
        else []
    )
    return {
        "selection_summary": (
            f"基于 {candidate_count} 位候选 KOL，按 kol_score_v2 八维加权评分"
            f"圈选 Top{selected_count} 名单（按互动量降序）。"
        ),
        "fit_findings": fit_findings,
        "risk_notes": risk_notes,
        "usage_advice": usage_advice,
    }


def _build_payload(
    *,
    scope: dict[str, Any],
    items: list[dict[str, Any]],
    candidate_count: int,
    data_as_of: datetime | None,
    source_names: tuple[str, ...],
) -> dict[str, Any]:
    selected_count = len(items)
    return {
        "schema_version": SCHEMA_VERSION,
        "module": "kol",
        "data_status": "complete",
        "availability": {
            "scoring": {"status": "complete", "reason_codes": []},
            "items": {"status": "complete", "reason_codes": []},
            "summary": {"status": "complete", "reason_codes": []},
        },
        "limitations": [],
        "methodology": methodology_dict(data_as_of=data_as_of, source_names=source_names),
        "scope": scope,
        "data": {
            "scoring": _scoring_block(),
            "items": items,
            "summary": {
                "candidate_count": candidate_count,
                "selected_count": selected_count,
                "platform_distribution": distribution([item["platform"] for item in items]),
                "rating_distribution": distribution(
                    [item["score_snapshot"]["rating"] for item in items]
                ),
            },
        },
        "narrative": _narrative(items, candidate_count),
    }


def _restricted_draft(
    scope: dict[str, Any],
    data_as_of: datetime | None,
    source_names: tuple[str, ...],
) -> DraftBuildResult:
    """数据不足（无候选）：必须 restricted 并披露限制（§12.1）。"""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "module": "kol",
        "data_status": "restricted",
        "availability": {
            "scoring": {"status": "complete", "reason_codes": []},
            "items": {"status": "unavailable", "reason_codes": ["no_kol_data"]},
            "summary": {"status": "unavailable", "reason_codes": ["no_kol_data"]},
        },
        "limitations": [
            {
                "code": "insufficient_kol_data",
                "message": "候选 KOL 数据不足，无法形成完整名单，数据受限披露",
                "affected_paths": ["summary.candidate_count", "summary.selected_count"],
            }
        ],
        "methodology": methodology_dict(data_as_of=data_as_of, source_names=source_names),
        "scope": scope,
        "data": {
            "scoring": _scoring_block(),
            "items": [],
            "summary": {
                "candidate_count": None,
                "selected_count": None,
                "platform_distribution": [],
                "rating_distribution": [],
            },
        },
        "narrative": {
            "selection_summary": "候选 KOL 数据不足，无法圈选名单。",
            "fit_findings": [],
            "risk_notes": [],
            "usage_advice": [],
        },
    }
    KolSelectionV3.model_validate(payload)
    return DraftBuildResult(
        module="kol-selection",
        schema_version=SCHEMA_VERSION,
        artifact_type=SCHEMA_VERSION,
        business_fields={"scope": scope},
        payload=payload,
        evidence_refs=[],
    )


async def _recorded_rank_kols_call_id(
    db: AsyncSession | None, context: ToolContext, args: RankKolsArgs
) -> str | None:
    """定位 builder 调用 rank_kols 后由运行时落库的 settled 调用行。"""
    if db is None or context.step_id is None:
        return None
    args_hash = arguments_hash(args.model_dump())
    logical = logical_call_id_for(context.run_id, context.step_id, RankKolsTool.name, args_hash)
    row = await db.scalar(select(AgentToolCall).where(AgentToolCall.logical_call_id == logical))
    return row.id if row is not None else None


# --------------------------------------------------------------------------- #
# Lineage：Evidence 引用 + settled rank_kols 派生
# --------------------------------------------------------------------------- #


def _ev(evidence_id: str, path: str) -> dict[str, Any]:
    return {"source_type": "evidence", "evidence_id": evidence_id, "source_path": path}


def _derivation(call_id: str | None, method: str, input_path: str) -> dict[str, Any] | None:
    if call_id is None:
        return None
    return {"tool_call_id": call_id, "method": method, "input_paths": [input_path]}


def _score_sources(evidence_id: str, raw_index: int, raw: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(raw.get("score_inputs"), dict):
        return [_ev(evidence_id, f"/{raw_index}/score_inputs")]
    # 兜底：平台是稳定身份字符串，必然存在于 Evidence 项内。
    return [_ev(evidence_id, f"/{raw_index}/platform")]


def _dim_source(
    evidence_id: str, raw_index: int, raw: dict[str, Any], input_key: str
) -> dict[str, Any]:
    score_inputs = raw.get("score_inputs")
    if isinstance(score_inputs, dict) and input_key in score_inputs:
        return _ev(evidence_id, f"/{raw_index}/score_inputs/{input_key}")
    return _score_sources(evidence_id, raw_index, raw)[0]


def _summary_lineage(
    payload: dict[str, Any],
    evidence_id: str,
    raw_mapping: list[int],
    call_id: str | None,
) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    items = payload["data"]["items"]
    if not items:
        return refs
    summary = payload["data"]["summary"]
    representative = _ev(evidence_id, f"/{raw_mapping[0]}/platform")

    for field in ("candidate_count", "selected_count"):
        refs.append(
            {
                "artifact_path": f"/data/summary/{field}",
                "sources": [representative],
                "derivation": _derivation(call_id, "rank_kols:count", representative["source_path"]),
            }
        )

    bucket_first: dict[Any, int] = {}
    for index, item in enumerate(items):
        key = item.get("platform")
        if key is not None:
            bucket_first.setdefault(key, index)
    for j, entry in enumerate(summary.get("platform_distribution") or ()):
        source_index = bucket_first.get(entry["key"])
        source = (
            _ev(evidence_id, f"/{raw_mapping[source_index]}/platform")
            if source_index is not None
            else representative
        )
        for suffix in ("count", "share"):
            refs.append(
                {
                    "artifact_path": f"/data/summary/platform_distribution/{j}/{suffix}",
                    "sources": [source],
                    "derivation": _derivation(
                        call_id, "kol_selection:distribution", source["source_path"]
                    ),
                }
            )

    bucket_first = {}
    for index, item in enumerate(items):
        key = item["score_snapshot"]["rating"]
        if key is not None:
            bucket_first.setdefault(key, index)
    for j, entry in enumerate(summary.get("rating_distribution") or ()):
        source_index = bucket_first.get(entry["key"])
        # rating 由评分派生，Evidence 项无 score_snapshot；引用稳定平台身份即可。
        source = (
            _ev(evidence_id, f"/{raw_mapping[source_index]}/platform")
            if source_index is not None
            else representative
        )
        for suffix in ("count", "share"):
            refs.append(
                {
                    "artifact_path": f"/data/summary/rating_distribution/{j}/{suffix}",
                    "sources": [source],
                    "derivation": _derivation(
                        call_id, "kol_selection:distribution", source["source_path"]
                    ),
                }
            )
    return refs


def _build_lineage(
    *,
    payload: dict[str, Any],
    evidence_id: str,
    raw_items: list[dict[str, Any]],
    raw_mapping: list[int],
    call_id: str | None,
) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    items = payload["data"]["items"]

    for index, item in enumerate(items):
        raw_index = raw_mapping[index]
        raw = raw_items[raw_index]

        # 展示/排序数字：直接复制自 Evidence。
        for field in _DISPLAY_NUMERIC_FIELDS:
            value = item.get(field)
            if value is None or field not in raw:
                continue
            refs.append(
                {
                    "artifact_path": f"/data/items/{index}/{field}",
                    "sources": [_ev(evidence_id, f"/{raw_index}/{field}")],
                    "derivation": None,
                }
            )

        # rank 由 engagement_total 降序排序得出；来源兜底到稳定身份字段。
        rank_source = _score_sources(evidence_id, raw_index, raw)[0]
        refs.append(
            {
                "artifact_path": f"/data/items/{index}/rank",
                "sources": [rank_source],
                "derivation": _derivation(
                    call_id, "rank_kols:engagement_total_desc", rank_source["source_path"]
                ),
            }
        )

        # 派生评分数字 → settled rank_kols 调用。
        score_sources = _score_sources(evidence_id, raw_index, raw)
        for field in ("total", "rating", "stars", "data_completeness"):
            refs.append(
                {
                    "artifact_path": f"/data/items/{index}/score_snapshot/{field}",
                    "sources": score_sources,
                    "derivation": _derivation(
                        call_id, "kol_score_v2", score_sources[0]["source_path"]
                    ),
                }
            )

        # 每个维度：原始输入引用 Evidence，派生结果引用 rank_kols。
        for dim in SCORE_DIMENSIONS:
            source = _dim_source(evidence_id, raw_index, raw, DIM_RAW_INPUT[dim])
            source_path = source["source_path"]
            for suffix in ("raw_score", "weighted_score"):
                refs.append(
                    {
                        "artifact_path": (
                            f"/data/items/{index}/score_snapshot/dimensions/{dim}/{suffix}"
                        ),
                        "sources": [source],
                        "derivation": _derivation(call_id, f"kol_score_v2:{dim}", source_path),
                    }
                )

    refs.extend(_summary_lineage(payload, evidence_id, raw_mapping, call_id))
    return refs


# --------------------------------------------------------------------------- #
# 公开入口
# --------------------------------------------------------------------------- #


async def build_kol_selection_draft(
    *,
    scope: dict[str, Any],
    evidence_id: str,
    items: list[dict[str, Any]],
    context: ToolContext,
    db: AsyncSession | None = None,
    data_as_of: datetime | None = None,
    source_names: tuple[str, ...] = ("kol_evidence",),
) -> DraftBuildResult:
    """把模型选定的 KOL 列表 Evidence 转换为 ``kol_selection_v3`` Draft。

    评分委托 ``rank_kols``（严格复用 ``kol_score_v2``），默认跨平台 Top20 按
    ``engagement_total`` 降序；数据不足时产出 restricted。
    """
    parsed_scope = KolSelectionScope.model_validate(scope)
    scope_dict = parsed_scope.model_dump()

    if not items:
        return _restricted_draft(scope_dict, data_as_of, source_names)

    args = RankKolsArgs(
        items=_rank_items(items),
        context=_rank_context(scope_dict),
        limit=DEFAULT_LIMIT,
    )
    result = await RankKolsTool(db).execute(context, args)
    if result.status != "success":
        raise DraftBuildError(f"rank_kols failed: {result.safe_summary}")
    ranked = json.loads(result.safe_summary)["items"]

    call_id = await _recorded_rank_kols_call_id(db, context, args)

    index_by_key = {
        (item.get("platform"), item.get("kol_uid")): index for index, item in enumerate(items)
    }
    payload_items: list[dict[str, Any]] = []
    raw_mapping: list[int] = []
    for entry in ranked:
        raw_index = index_by_key.get((entry.get("platform"), entry.get("kol_uid")), 0)
        raw_mapping.append(raw_index)
        payload_items.append(_build_item(entry, items[raw_index]))

    payload = _build_payload(
        scope=scope_dict,
        items=payload_items,
        candidate_count=len(items),
        data_as_of=data_as_of,
        source_names=source_names,
    )
    KolSelectionV3.model_validate(payload)  # fail-fast：builder 输出必须合法。

    refs = _build_lineage(
        payload=payload,
        evidence_id=evidence_id,
        raw_items=items,
        raw_mapping=raw_mapping,
        call_id=call_id,
    )

    return DraftBuildResult(
        module="kol-selection",
        schema_version=SCHEMA_VERSION,
        artifact_type=SCHEMA_VERSION,
        business_fields={"scope": scope_dict},
        payload=payload,
        evidence_refs=refs,
        rank_kols_call_id=call_id,
    )


__all__ = [
    "DIM_RAW_INPUT",
    "DEFAULT_LIMIT",
    "SCHEMA_VERSION",
    "build_kol_selection_draft",
]
