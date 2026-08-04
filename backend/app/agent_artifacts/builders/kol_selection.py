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
- 真实 Evidence 行（中英混合键，如 kol_xiaohongshu_search 的「KOL 列表」行）
  先经 :func:`_normalize_kol_row` 归一为契约键：平台恒为字符串（缺失取 scope
  唯一平台，再缺省 ``unknown``），uid 缺失回退昵称，数值字段按别名解析；
- 每个维度的原始输入引用 Evidence，派生评分（raw_score/weighted_score/total/
  rating/stars/data_completeness）引用已 settled 的 ``rank_kols`` 调用；
  归一字段的 lineage 指向 Evidence 行内真实存在的原始键。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_artifacts.builders.common import (
    DraftBuildError,
    DraftBuildResult,
    distribution,
    methodology_dict,
)
from app.agent_artifacts.builders.raw_rows import (
    AGGREGATE_PLATFORM_NAMES,
    PLATFORM_KEYS,
    canon_platform,
    join_source_path,
    num,
    text,
    valid_url,
    whole,
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

# 真实 Evidence 行的中英字段别名（kol_xiaohongshu_search / social_statistic_hot_user
# 等 MCP 结果的原始行键）；英文契约键在首位，已归一的项原样通过。
_KOL_UID_KEYS = (
    "kol_uid",
    "账号ID (kwUid)",
    "账号ID",
    "kwUid",
    "达人ID",
    "author_id",
    "uid",
    "用户ID",
    "用户id",
    "小红书id",
)
_NICKNAME_KEYS = ("nickname", "昵称", "用户昵称", "达人昵称", "作者", "author")
_FOLLOWERS_KEYS = ("followers", "粉丝数")
_ACTIVE_FOLLOWERS_KEYS = ("active_followers", "有效粉丝数")
_ACTIVE_FOLLOWER_RATE_KEYS = ("active_follower_rate", "有效粉丝率")
_ENGAGEMENT_TOTAL_KEYS = (
    "engagement_total",
    "平均互动",
    "互动数",
    "互动量",
    "互动",
    "engagement",
    "interactions",
)
_AVG_ENGAGEMENT_KEYS = ("avg_engagement", "平均互动")
_GROWTH_RATE_KEYS = ("growth_rate", "周粉丝增长率", "月粉丝增长率")
_QUOTED_PRICE_KEYS = ("quoted_price", "预估报价-图文", "预估报价-视频", "报价")
_LIKES_KEYS = ("likes", "平均点赞", "点赞数", "点赞")
_COMMENTS_KEYS = ("comments", "平均评论", "评论数", "评论")
_SHARES_KEYS = ("shares", "平均转发", "分享数", "转发数", "转发", "分享")
_AVATAR_URL_KEYS = ("avatar_url", "头像", "用户头像")
_HOMEPAGE_URL_KEYS = ("homepage_url", "主页", "用户主页链接", "主页链接")

_UNKNOWN_PLATFORM = "unknown"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


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


def _first_present(row: dict[str, Any], keys: tuple[str, ...]) -> tuple[str | None, Any]:
    for key in keys:
        if key in row and row[key] is not None and row[key] != "":
            return key, row[key]
    return None, None


def _normalize_kol_row(
    row: dict[str, Any], *, default_platform: str | None
) -> tuple[dict[str, Any], dict[str, str]]:
    """真实 Evidence 行（中英混合键）→ builder 契约键。

    返回 ``(归一项, 契约字段 → 原始行键)``：归一项保留原行全部键并写入契约
    键；keymap 供 lineage 指向 Evidence 行内真实存在的键。平台恒为字符串：
    平台字段缺失或非字符串时取 scope 唯一平台，再缺省 ``"unknown"``——绝不
    产出 None/非字符串（第三轮 UAT 的 items.platform 校验失败根因）。
    """
    item = dict(row)
    keymap: dict[str, str] = {}

    def _adopt_text(field: str, keys: tuple[str, ...]) -> str | None:
        existing = item.get(field)
        if isinstance(existing, str) and existing.strip():
            keymap[field] = field
            return existing.strip()
        raw_key, raw_value = _first_present(row, keys)
        # 非标量（dict/list/bool）不字符串化，按缺失处理。
        if raw_key is None or not isinstance(raw_value, (str, int, float)):
            return None
        if isinstance(raw_value, bool):
            return None
        rendered = text(raw_value)
        if rendered is None:
            return None
        keymap[field] = raw_key
        return rendered

    def _adopt_number(field: str, keys: tuple[str, ...], *, integer: bool) -> Any:
        existing = num(item.get(field))
        if existing is not None:
            keymap[field] = field
            return whole(existing) if integer else existing
        raw_key, raw_value = _first_present(row, keys)
        parsed = num(raw_value)
        if raw_key is None or parsed is None:
            return None
        keymap[field] = raw_key
        return whole(parsed) if integer else parsed

    def _adopt_url(field: str, keys: tuple[str, ...]) -> str | None:
        existing = valid_url(item.get(field))
        if existing is not None:
            keymap[field] = field
            return existing
        raw_key, raw_value = _first_present(row, keys)
        url = valid_url(raw_value)
        if raw_key is None or url is None:
            return None
        keymap[field] = raw_key
        return url

    platform_text = _adopt_text("platform", PLATFORM_KEYS)
    platform = canon_platform(platform_text) if platform_text else ""
    if not platform or platform in AGGREGATE_PLATFORM_NAMES:
        # 平台缺失/合计值：从 scope 唯一平台推断，无法推断明确标 unknown；
        # 推断值不指向任何原始行键，lineage 不登记。
        platform = default_platform or _UNKNOWN_PLATFORM
        keymap.pop("platform", None)
    item["platform"] = platform

    kol_uid = _adopt_text("kol_uid", _KOL_UID_KEYS)
    nickname = _adopt_text("nickname", _NICKNAME_KEYS)
    if kol_uid is None:
        # uid 缺失回退昵称作身份（与 campaign kol_contributions 口径一致）。
        kol_uid = nickname or ""
        if nickname is not None:
            keymap["kol_uid"] = keymap["nickname"]
    item["kol_uid"] = kol_uid
    item["nickname"] = nickname if nickname is not None else kol_uid

    for field, keys, integer in (
        ("followers", _FOLLOWERS_KEYS, True),
        ("active_followers", _ACTIVE_FOLLOWERS_KEYS, True),
        ("active_follower_rate", _ACTIVE_FOLLOWER_RATE_KEYS, False),
        ("growth_rate", _GROWTH_RATE_KEYS, False),
        ("engagement_total", _ENGAGEMENT_TOTAL_KEYS, True),
        ("avg_engagement", _AVG_ENGAGEMENT_KEYS, False),
        ("likes", _LIKES_KEYS, True),
        ("comments", _COMMENTS_KEYS, True),
        ("shares", _SHARES_KEYS, True),
        ("quoted_price", _QUOTED_PRICE_KEYS, True),
    ):
        value = _adopt_number(field, keys, integer=integer)
        if value is not None:
            item[field] = value

    for field, keys in (
        ("avatar_url", _AVATAR_URL_KEYS),
        ("homepage_url", _HOMEPAGE_URL_KEYS),
    ):
        url = _adopt_url(field, keys)
        if url is not None:
            item[field] = url

    return item, keymap


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


def _narrative(
    items: list[dict[str, Any]], candidate_count: int, *, sort_missing: bool = False
) -> dict[str, Any]:
    selected_count = len(items)
    if sort_missing:
        selection_summary = (
            f"基于 {candidate_count} 位候选 KOL，按 kol_score_v2 八维加权评分圈选 "
            f"Top{selected_count}；候选缺少互动量数据，无法按互动量降序，名单按原始"
            f"顺序展示（数据受限）。"
        )
    else:
        selection_summary = (
            f"基于 {candidate_count} 位候选 KOL，按 kol_score_v2 八维加权评分"
            f"圈选 Top{selected_count} 名单（按互动量降序）。"
        )
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
    if sort_missing:
        risk_notes.append(
            {
                "text": "所有候选均缺少互动量数据，名单排序不代表互动量高低。",
                "kol_uid": None,
                "supporting_paths": ["data.items.0.engagement_total"],
            }
        )
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
        "selection_summary": selection_summary,
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
    sort_missing: bool = False,
    narrative: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """组装名单 payload；``sort_missing`` 时按互动量排序不可用 → restricted。

    数据存在但排序键（``engagement_total``）完全缺失时，rank_kols 只能按稳定
    输入顺序输出（任意顺序），不得声称「按互动量降序」——必须受限披露。

    §6.3 递归 null 治理：``items[]`` 的 Optional 展示数值（followers/
    quoted_price 等）出现 null 时同样必须受限披露（items partial +
    limitation），不得谎称 complete。

    ``narrative``：模型提供的叙事（设计 §6.1），缺省时由 ``_narrative``
    按评分结果确定性生成兜底；模型叙事的 supporting_paths 是否指向 data
    内真实路径由下游 ``KolSelectionV3.model_validate`` 统一校验。
    """
    selected_count = len(items)
    null_display = [
        f"items.{index}.{field}"
        for index, item in enumerate(items)
        for field in _DISPLAY_NUMERIC_FIELDS
        if item.get(field) is None
    ]
    items_restricted = sort_missing or bool(null_display)
    reason_codes: list[str] = []
    if sort_missing:
        reason_codes.append("sort_key_missing")
    if null_display:
        reason_codes.append("metric_data_missing")
    limitations: list[dict[str, Any]] = []
    if sort_missing:
        limitations.append(
            {
                "code": "sort_key_missing",
                "message": "候选 KOL 缺少互动量数据，无法按互动量排序，名单按原始顺序展示",
                "affected_paths": ["items"],
            }
        )
    if null_display:
        limitations.append(
            {
                "code": "metric_data_missing",
                "message": "部分达人展示指标缺失，数据受限披露",
                "affected_paths": null_display,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "module": "kol",
        "data_status": "restricted" if items_restricted else "complete",
        "availability": {
            "scoring": {"status": "complete", "reason_codes": []},
            "items": {
                "status": "partial" if items_restricted else "complete",
                "reason_codes": reason_codes,
            },
            "summary": {"status": "complete", "reason_codes": []},
        },
        "limitations": limitations,
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
        "narrative": narrative
        if narrative is not None
        else _narrative(items, candidate_count, sort_missing=sort_missing),
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
    try:
        KolSelectionV3.model_validate(payload)
    except ValidationError as exc:
        raise DraftBuildError(f"invalid kol_selection_v3 payload: {exc}") from exc
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


def _identity_source(
    evidence_id: str, base: str, raw: dict[str, Any], keymap: dict[str, str]
) -> dict[str, Any]:
    """稳定身份引用：平台/uid 原始行键优先，推断值兜底到行内首个键。

    ``base`` 是行在 Evidence raw payload 内的基准 JSON Pointer（含容器键
    前缀，如 ``/KOL 列表/52``；``{"result": "<json>"}`` 包装时为 ``/result``）。
    """
    for field in ("platform", "kol_uid"):
        raw_key = keymap.get(field)
        if raw_key is not None and raw_key in raw:
            return _ev(evidence_id, join_source_path(base, raw_key))
    return _ev(evidence_id, join_source_path(base, next(iter(raw))))


def _score_sources(
    evidence_id: str, base: str, raw: dict[str, Any], keymap: dict[str, str]
) -> list[dict[str, Any]]:
    if isinstance(raw.get("score_inputs"), dict):
        return [_ev(evidence_id, join_source_path(base, "score_inputs"))]
    return [_identity_source(evidence_id, base, raw, keymap)]


def _dim_source(
    evidence_id: str,
    base: str,
    raw: dict[str, Any],
    keymap: dict[str, str],
    input_key: str,
) -> dict[str, Any]:
    score_inputs = raw.get("score_inputs")
    if isinstance(score_inputs, dict) and input_key in score_inputs:
        return _ev(evidence_id, join_source_path(base, "score_inputs", input_key))
    return _score_sources(evidence_id, base, raw, keymap)[0]


def _summary_lineage(
    payload: dict[str, Any],
    evidence_id: str,
    raw_items: list[dict[str, Any]],
    raw_mapping: list[int],
    keymaps: list[dict[str, str]],
    bases: list[str],
    call_id: str | None,
) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    items = payload["data"]["items"]
    if not items:
        return refs
    summary = payload["data"]["summary"]
    representative = _identity_source(
        evidence_id, bases[raw_mapping[0]], raw_items[raw_mapping[0]], keymaps[raw_mapping[0]]
    )

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
            _identity_source(
                evidence_id,
                bases[raw_mapping[source_index]],
                raw_items[raw_mapping[source_index]],
                keymaps[raw_mapping[source_index]],
            )
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
            _identity_source(
                evidence_id,
                bases[raw_mapping[source_index]],
                raw_items[raw_mapping[source_index]],
                keymaps[raw_mapping[source_index]],
            )
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
    keymaps: list[dict[str, str]],
    bases: list[str],
    call_id: str | None,
) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    items = payload["data"]["items"]

    for index, item in enumerate(items):
        raw_index = raw_mapping[index]
        raw = raw_items[raw_index]
        keymap = keymaps[raw_index]
        base = bases[raw_index]

        # 展示/排序数字：直接复制自 Evidence；lineage 指向原始行键（中文别名
        # 归一的字段指向真实存在的中文键，而非归一后的契约键）。路径 = 行的
        # 基准指针（含容器键前缀）+ 原始行键。
        for field in _DISPLAY_NUMERIC_FIELDS:
            value = item.get(field)
            raw_key = keymap.get(field)
            if value is None or raw_key is None or raw_key not in raw:
                continue
            refs.append(
                {
                    "artifact_path": f"/data/items/{index}/{field}",
                    "sources": [_ev(evidence_id, join_source_path(base, raw_key))],
                    "derivation": None,
                }
            )

        # rank 由 engagement_total 降序排序得出；来源兜底到稳定身份字段。
        rank_source = _score_sources(evidence_id, base, raw, keymap)[0]
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
        score_sources = _score_sources(evidence_id, base, raw, keymap)
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
            source = _dim_source(evidence_id, base, raw, keymap, DIM_RAW_INPUT[dim])
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

    refs.extend(
        _summary_lineage(payload, evidence_id, raw_items, raw_mapping, keymaps, bases, call_id)
    )
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
    row_source_paths: list[str] | None = None,
    narrative: dict[str, Any] | None = None,
) -> DraftBuildResult:
    """把模型选定的 KOL 列表 Evidence 转换为 ``kol_selection_v3`` Draft。

    评分委托 ``rank_kols``（严格复用 ``kol_score_v2``），默认跨平台 Top20 按
    ``engagement_total`` 降序；数据不足时产出 restricted。

    ``narrative``：模型提供的叙事（设计 §6.1），写入 payload 前经
    ``KolSelectionV3.model_validate`` 强校验（含 supporting_paths 必须指向
    data 内真实路径）；缺省时按评分结果确定性生成兜底叙事。无候选的
    restricted 路径恒用 builder 自己的受限披露叙事（此时无 data 可引用，
    不采用模型叙事）。

    ``row_source_paths``：与 ``items`` 等长平行的行基准 JSON Pointer（行在
    Evidence raw payload 内的位置，含容器键前缀，通常取
    ``extract_rows`` 的 ``RowRef.field_base``）。缺省按顶层数组 ``/{index}``
    处理（兼容直接传行的调用方）；长度不一致直接 fail-fast，不静默错位。
    """
    parsed_scope = KolSelectionScope.model_validate(scope)
    scope_dict = parsed_scope.model_dump()

    if not items:
        return _restricted_draft(scope_dict, data_as_of, source_names)

    if row_source_paths is None:
        bases = [f"/{index}" for index in range(len(items))]
    else:
        if len(row_source_paths) != len(items):
            raise DraftBuildError(
                "row_source_paths must be parallel to items "
                f"({len(row_source_paths)} != {len(items)})"
            )
        bases = list(row_source_paths)

    # 真实 Evidence 行归一：中文原始键 → 契约键，平台恒为字符串（缺失时取
    # scope 唯一平台，再缺省 unknown）；keymap 供 lineage 指向原始行键。
    scope_platforms = list(scope_dict.get("platforms") or ())
    default_platform = canon_platform(scope_platforms[0]) if len(scope_platforms) == 1 else None
    normalized_items: list[dict[str, Any]] = []
    keymaps: list[dict[str, str]] = []
    for row in items:
        normalized_row, keymap = _normalize_kol_row(row, default_platform=default_platform)
        normalized_items.append(normalized_row)
        keymaps.append(keymap)
    items = normalized_items

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

    # 排序键（engagement_total）完全缺失时，rank_kols 只能按稳定输入顺序输出，
    # 顺序不具业务含义——必须受限披露，且与分析 builder（total_engagement 为 None
    # 即 restricted）行为一致。
    sort_missing = not any(_is_number(item.get("engagement_total")) for item in items)

    payload = _build_payload(
        scope=scope_dict,
        items=payload_items,
        candidate_count=len(items),
        data_as_of=data_as_of,
        source_names=source_names,
        sort_missing=sort_missing,
        narrative=narrative,
    )
    try:
        KolSelectionV3.model_validate(payload)  # fail-fast：builder 输出必须合法。
    except ValidationError as exc:
        raise DraftBuildError(f"invalid kol_selection_v3 payload: {exc}") from exc

    refs = _build_lineage(
        payload=payload,
        evidence_id=evidence_id,
        raw_items=items,
        raw_mapping=raw_mapping,
        keymaps=keymaps,
        bases=bases,
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
