from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Mapping, Sequence
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.selection.models import KolSelectionDetailView
from app.selection.normalizers import normalize_kol_detail_facts


_DETAIL_NUMBER_FIELDS = {
    "followers",
    "quoted_price_cny",
    "engagement_rate",
    "content_score",
    "average_interactions",
    "recent_30d_average_interactions",
    "effective_follower_rate",
    "active_follower_count",
    "works_count",
}
_DETAIL_TEXT_FIELDS = {"profile_url", "city", "nickname"}
_DETAIL_DISTRIBUTION_FIELDS = {"audience_age", "audience_regions", "audience_interests"}
_DETAIL_TREND_FIELDS = {"trend_points"}
_POST_FIELDS = {
    "title",
    "nickname",
    "interact",
    "like",
    "comment",
    "collect",
    "publish_time",
    "url",
    "platform",
}


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return value
    return None


def _record(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        return dict(dumped) if isinstance(dumped, Mapping) else {}
    return {}


def normalize_detail_view_payload(detail: Mapping[str, Any]) -> dict[str, Any]:
    """只持久化详情 BI 所需的规范字段，过滤不受信任的原始 MCP 载荷。"""
    normalized: dict[str, Any] = {}
    for key in _DETAIL_NUMBER_FIELDS:
        value = _number(detail.get(key))
        if value is not None and value >= 0:
            normalized[key] = value
    for key in _DETAIL_TEXT_FIELDS:
        value = detail.get(key)
        if isinstance(value, str) and value.strip():
            normalized[key] = value.strip()
    for key in _DETAIL_DISTRIBUTION_FIELDS:
        value = detail.get(key)
        if not isinstance(value, Mapping):
            continue
        distribution = {
            str(name): number
            for name, item in value.items()
            if str(name).strip() and (number := _number(item)) is not None and number >= 0
        }
        if distribution:
            normalized[key] = distribution
    trend_points = detail.get("trend_points")
    if isinstance(trend_points, Sequence) and not isinstance(trend_points, str | bytes):
        normalized_points = [
            {
                field: value
                for field, value in _record(point).items()
                if field in {"week_start", "average_interactions", "post_count"}
                and (isinstance(value, str) or _number(value) is not None)
            }
            for point in trend_points
        ]
        normalized_points = [point for point in normalized_points if point]
        if normalized_points:
            normalized["trend_points"] = normalized_points
    return normalized


def normalize_mcp_detail_view_payload(
    *, platform: str, kol_uid: str, detail: Mapping[str, Any]
) -> dict[str, Any]:
    """将快捷详情工具返回的原始行投影为详情缓存白名单字段。

    QuickService 已负责模型驱动的工具调用与计费；这里复用名单详情快照的
    归一化器，保证原始供应商字段不会直接进入版本化缓存。
    """
    details = normalize_kol_detail_facts(
        "datatap.social.grow.kol.detail.v1",
        {
            "platform": platform,
            "kwUidList": [kol_uid],
            "scope": ["fansAudience", "postSummaryStatistics", "accountTrend"],
        },
        {"result": dict(detail)},
    )
    matched = next(
        (
            item
            for item in details
            if item.platform == platform and item.platform_account_id == kol_uid
        ),
        None,
    )
    if matched is None:
        return {}
    return normalize_detail_view_payload(
        {**matched.facts, "trend_points": list(matched.trend_points)}
    )


def normalize_detail_view_posts(posts: Sequence[Any]) -> list[dict[str, Any]]:
    """热帖只保存 UI 合约字段；最多五条由 Store 统一截断。"""
    normalized: list[dict[str, Any]] = []
    for source in posts:
        raw = _record(source)
        post: dict[str, Any] = {}
        for key in _POST_FIELDS:
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                post[key] = value.strip()
            elif _number(value) is not None and _number(value) >= 0:
                post[key] = _number(value)
        if post:
            normalized.append(post)
    return normalized[:5]


class DetailViewStore:
    """名单版本级详情缓存的窄读写接口。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get(
        self, *, selection_set_id: str, platform: str, kol_uid: str
    ) -> KolSelectionDetailView | None:
        return await self._db.scalar(
            select(KolSelectionDetailView).where(
                KolSelectionDetailView.selection_set_id == selection_set_id,
                KolSelectionDetailView.platform == platform,
                KolSelectionDetailView.kol_uid == kol_uid,
            )
        )

    async def upsert(
        self,
        *,
        selection_set_id: str,
        platform: str,
        kol_uid: str,
        detail: Mapping[str, Any],
        posts: Sequence[Any],
        points_cost: int,
        posts_degraded: bool,
    ) -> KolSelectionDetailView:
        if not selection_set_id or not platform.strip() or not kol_uid.strip():
            raise ValueError("detail_view_identity_required")
        if points_cost < 0:
            raise ValueError("detail_view_points_cost_invalid")
        existing = await self._db.scalar(
            select(KolSelectionDetailView)
            .where(
                KolSelectionDetailView.selection_set_id == selection_set_id,
                KolSelectionDetailView.platform == platform,
                KolSelectionDetailView.kol_uid == kol_uid,
            )
            .with_for_update()
        )
        now = _now()
        values = {
            "detail_json": normalize_detail_view_payload(detail),
            "posts_json": normalize_detail_view_posts(posts),
            "points_cost": points_cost,
            "posts_degraded": posts_degraded,
            "fetched_at": now,
            "updated_at": now,
        }
        if existing is None:
            existing = KolSelectionDetailView(
                id=str(uuid4()),
                selection_set_id=selection_set_id,
                platform=platform,
                kol_uid=kol_uid,
                created_at=now,
                **values,
            )
            self._db.add(existing)
        else:
            for key, value in values.items():
                setattr(existing, key, value)
        await self._db.flush()
        return existing
