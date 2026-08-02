"""``kol_detail_v2`` Draft builder（设计 §12.1 / §13.2 / Task 17）。

把已抓取的 KOL 详情 Evidence（identity / metrics / audience / trend /
latest_posts）转换为强类型 ``kol_detail_v2`` Draft：
- ``latest_posts`` 最多 5 条；``homepage_url`` / 帖子 ``url`` 缺失或非 http(s)
  时披露 limitation（restricted），不伪造链接，前端显示不可用；
- 必需章节（identity/metrics/audience/trend/latest_posts）任一 partial /
  unavailable 时 ``data_status=restricted`` 并给出 limitation；
- ``data.cache`` 由调用方传入的 ``cache_state``（fetched_at/expires_at）填充，
  属运行时元数据，不要求 lineage；
- lineage 对 ``data`` 下每个非空数值叶子给出 Evidence 引用（§10.4），
  source_path 与 Evidence raw payload 的结构一一对应。

builder 只做确定性转换，不调用 MCP。

注意（设计确认，非强制接线）：kol_detail_v1 运行时由模型经 Task 14 引擎内联
产出 kol_detail_v2 并经 Reviewer（Task 13）把关，本 builder 不在此路径上强制
调用——它供缓存命中重建与 Draft 工具做确定性转换（与 Task 16 其它 builder
一致），引擎不强制注入 builder。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError

from app.agent_artifacts.builders.common import (
    DraftBuildError,
    DraftBuildResult,
    methodology_dict,
)
from app.agent_artifacts.payloads.kol_detail import KolDetailV2

SCHEMA_VERSION = "kol_detail_v2"

# 最新热帖上限（§12.1 kol_detail_v2）。
_MAX_POSTS = 5

_METRIC_FIELDS = (
    "followers",
    "following",
    "posts",
    "likes",
    "active_followers",
    "active_follower_rate",
    "growth_rate",
    "engagement_total",
    "avg_engagement",
)

_AUDIENCE_FIELDS = (
    "gender_distribution",
    "age_distribution",
    "region_distribution",
    "interest_distribution",
)

# 必需章节：任一非 complete 都必须 restricted 并披露 limitation（§12.1 聚合规则）。
_REQUIRED_SECTIONS = ("identity", "metrics", "audience", "trend", "latest_posts")

_LIMITATION_MESSAGE = {
    "homepage_url_missing": "未获取到达人主页链接，前端展示不可用",
    "metric_data_missing": "达人部分核心指标缺失，数据受限披露",
    "audience_missing": "未获取到达人受众画像，数据受限披露",
    "audience_partial": "达人部分受众分布缺失，数据受限披露",
    "trend_missing": "未获取到达人趋势数据，数据受限披露",
    "latest_posts_missing": "未获取到达人最新热帖，数据受限披露",
    "post_url_missing": "部分热帖缺少原帖链接，前端展示不可用",
}


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _iso(value: Any) -> Any:
    """datetime/date → ISO 字符串，保证 JSON 可落库；字符串原样透传。"""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _sanitize_url(value: Any) -> str | None:
    """只保留 http/https 且带主机名的 URL；缺失/非法 scheme/空 host 一律按缺失处理。"""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    if not parts.hostname:
        return None
    return value


def _build_data(detail: dict[str, Any], cache_state: dict[str, Any]) -> dict[str, Any]:
    raw_identity = detail.get("identity") or {}
    raw_metrics = detail.get("metrics") or {}
    raw_audience = detail.get("audience") or {}
    raw_trend = detail.get("trend") or ()
    raw_posts = detail.get("latest_posts") or ()

    identity = {
        "nickname": str(raw_identity.get("nickname") or ""),
        "avatar_url": _sanitize_url(raw_identity.get("avatar_url")),
        "homepage_url": _sanitize_url(raw_identity.get("homepage_url")),
        "bio": str(raw_identity.get("bio") or ""),
        "verification": bool(raw_identity.get("verification")),
        "region": str(raw_identity.get("region") or ""),
    }
    metrics = {field: raw_metrics.get(field) for field in _METRIC_FIELDS}
    audience = {
        field: [
            {
                "key": str(item.get("key") or ""),
                "label": str(item.get("label") or item.get("key") or ""),
                "value": item.get("value"),
                "share": item.get("share"),
            }
            for item in (raw_audience.get(field) or [])
        ]
        for field in _AUDIENCE_FIELDS
    }
    trend = [
        {
            "date": _iso(item.get("date")),
            "followers": item.get("followers"),
            "engagement": item.get("engagement"),
            "posts": item.get("posts"),
        }
        for item in raw_trend
    ]
    latest_posts = [
        {
            "post_id": str(item.get("post_id") or ""),
            "title": str(item.get("title") or ""),
            "url": _sanitize_url(item.get("url")),
            "published_at": _iso(item.get("published_at")),
            "likes": item.get("likes"),
            "comments": item.get("comments"),
            "shares": item.get("shares"),
            "engagement": item.get("engagement"),
        }
        for item in raw_posts
    ][:_MAX_POSTS]
    cache = {
        "hit": bool(cache_state.get("hit", False)),
        "fetched_at": _iso(cache_state.get("fetched_at")),
        "expires_at": _iso(cache_state.get("expires_at")),
    }
    return {
        "identity": identity,
        "metrics": metrics,
        "audience": audience,
        "trend": trend,
        "latest_posts": latest_posts,
        "cache": cache,
    }


def _section_plan(data: dict[str, Any]) -> dict[str, tuple[str, tuple[str, ...], tuple[str, ...]]]:
    """返回 {必需章节: (status, reason_codes, affected_paths)}。"""
    plan: dict[str, tuple[str, tuple[str, ...], tuple[str, ...]]] = {}

    identity = data["identity"]
    if identity.get("homepage_url") is None:
        plan["identity"] = ("partial", ("homepage_url_missing",), ("identity.homepage_url",))
    else:
        plan["identity"] = ("complete", (), ())

    metrics = data["metrics"]
    null_fields = [field for field in _METRIC_FIELDS if metrics.get(field) is None]
    if len(null_fields) == len(_METRIC_FIELDS):
        plan["metrics"] = (
            "unavailable",
            ("metric_data_missing",),
            tuple(f"metrics.{field}" for field in _METRIC_FIELDS),
        )
    elif null_fields:
        plan["metrics"] = (
            "partial",
            ("metric_data_missing",),
            tuple(f"metrics.{field}" for field in null_fields),
        )
    else:
        plan["metrics"] = ("complete", (), ())

    non_empty = [field for field in _AUDIENCE_FIELDS if data["audience"].get(field)]
    if not non_empty:
        plan["audience"] = ("unavailable", ("audience_missing",), ())
    elif len(non_empty) < len(_AUDIENCE_FIELDS):
        # 部分受众分布缺失：partial 披露，避免把「缺失」当「完整」。
        missing = [field for field in _AUDIENCE_FIELDS if not data["audience"].get(field)]
        plan["audience"] = (
            "partial",
            ("audience_partial",),
            tuple(f"audience.{field}" for field in missing),
        )
    else:
        plan["audience"] = ("complete", (), ())

    if data["trend"]:
        plan["trend"] = ("complete", (), ())
    else:
        plan["trend"] = ("unavailable", ("trend_missing",), ())

    posts = data["latest_posts"]
    if not posts:
        plan["latest_posts"] = ("unavailable", ("latest_posts_missing",), ())
    else:
        missing_urls = tuple(i for i, post in enumerate(posts) if post.get("url") is None)
        if missing_urls:
            plan["latest_posts"] = (
                "partial",
                ("post_url_missing",),
                tuple(f"latest_posts.{i}.url" for i in missing_urls),
            )
        else:
            plan["latest_posts"] = ("complete", (), ())

    return plan


def _narrative(data: dict[str, Any]) -> dict[str, Any]:
    identity = data["identity"]
    metrics = data["metrics"]
    posts = data["latest_posts"]

    content_strengths: list[dict[str, Any]] = []
    if metrics.get("engagement_total") is not None:
        content_strengths.append(
            {
                "title": "互动表现",
                "detail": "达人互动数据完整，可作为投放参考",
                "supporting_paths": ["metrics.engagement_total"],
            }
        )
    commercial_notes: list[dict[str, Any]] = []
    if posts:
        commercial_notes.append(
            {
                "title": "热帖表现",
                "detail": "已获取达人最近热帖，可用于内容参考",
                "supporting_paths": ["latest_posts.0.engagement"],
            }
        )
    risk_notes: list[dict[str, Any]] = []
    if identity.get("homepage_url") is None:
        risk_notes.append(
            {
                "title": "主页链接缺失",
                "detail": "未获取到达人主页链接，展示时显示不可用",
                "supporting_paths": ["identity.homepage_url"],
            }
        )
    if posts and any(post.get("url") is None for post in posts):
        risk_notes.append(
            {
                "title": "部分热帖缺少链接",
                "detail": "部分热帖未获取到原帖链接，展示时显示不可用",
                "supporting_paths": ["latest_posts.0.url"],
            }
        )
    return {
        "profile_summary": f"{identity['nickname']} 的达人详情概览",
        "content_strengths": content_strengths,
        "commercial_notes": commercial_notes,
        "risk_notes": risk_notes,
    }


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _lineage_refs(payload: dict[str, Any], evidence_id: str) -> list[dict[str, Any]]:
    """data 下每个非空数值叶子的 Evidence 引用；source_path 与 Evidence 结构一一对应。

    ``data.cache`` 是运行时元数据（hit 布尔 + 时间戳），无 Evidence 基座，跳过。
    """
    data = payload["data"]
    refs: list[dict[str, Any]] = []

    def walk(node: Any, parts: list[str]) -> None:
        if isinstance(node, dict):
            if parts and parts[-1] == "cache":
                return
            for key, value in node.items():
                walk(value, [*parts, key])
        elif isinstance(node, (list, tuple)):
            for index, item in enumerate(node):
                walk(item, [*parts, str(index)])
        elif _is_number(node):
            refs.append(
                {
                    "artifact_path": "/" + "/".join(_escape(part) for part in parts),
                    "sources": [
                        {
                            "source_type": "evidence",
                            "evidence_id": evidence_id,
                            "source_path": "/"
                            + "/".join(_escape(part) for part in parts[1:]),
                        }
                    ],
                    "derivation": None,
                }
            )

    walk(data, ["data"])
    return refs


def build_kol_detail_draft(
    *,
    platform: str,
    kol_uid: str,
    detail: dict[str, Any],
    evidence_id: str,
    cache_state: dict[str, Any],
    selection_artifact_id: str | None = None,
    selection_version: str | None = None,
    data_as_of: datetime | None = None,
    source_names: tuple[str, ...] = ("kol_evidence",),
) -> DraftBuildResult:
    """把已抓取的 KOL 详情 Evidence 转换为 ``kol_detail_v2`` Draft。

    ``cache_state`` 提供 ``hit/fetched_at/expires_at``（运行时元数据）；
    ``detail`` 的嵌套结构与 kol_detail_v2 的 ``data`` 章节一致，供 lineage
    的 source_path 一一对应。
    """
    data = _build_data(detail, cache_state)
    plan = _section_plan(data)

    availability: dict[str, Any] = {}
    limitations: list[dict[str, Any]] = []
    for section in _REQUIRED_SECTIONS:
        status, reasons, paths = plan[section]
        availability[section] = {
            "status": status,
            "reason_codes": list(reasons),
        }
        if status != "complete":
            code = reasons[0]
            limitations.append(
                {
                    "code": code,
                    "message": _LIMITATION_MESSAGE[code],
                    "affected_paths": list(paths),
                }
            )
    availability["cache"] = {"status": "complete", "reason_codes": []}

    data_status = (
        "restricted"
        if any(status != "complete" for status, _, _ in plan.values())
        else "complete"
    )
    scope = {
        "platform": platform,
        "kol_uid": kol_uid,
        "selection_artifact_id": selection_artifact_id,
        "selection_version": selection_version,
    }
    payload = {
        "schema_version": SCHEMA_VERSION,
        "module": "kol",
        "data_status": data_status,
        "availability": availability,
        "limitations": limitations,
        "methodology": methodology_dict(data_as_of=data_as_of, source_names=source_names),
        "scope": scope,
        "data": data,
        "narrative": _narrative(data),
    }
    try:
        KolDetailV2.model_validate(payload)  # fail-fast：builder 输出必须合法。
    except ValidationError as exc:
        raise DraftBuildError(f"invalid kol_detail_v2 payload: {exc}") from exc

    return DraftBuildResult(
        module="kol-detail",
        schema_version=SCHEMA_VERSION,
        artifact_type=SCHEMA_VERSION,
        business_fields={"platform": platform, "kol_uid": kol_uid},
        payload=payload,
        evidence_refs=_lineage_refs(payload, evidence_id),
    )


__all__ = ["SCHEMA_VERSION", "build_kol_detail_draft"]
