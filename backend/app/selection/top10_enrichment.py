"""Top20 达人详情补全的纯排序与批量分组逻辑。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable


_MAX_TARGETS = 20
DETAIL_SCOPES = ("fansAudience", "postSummaryStatistics", "accountTrend")


@dataclass(frozen=True)
class DetailEnrichmentTarget:
    """一位需要批量 ``kol.detail`` 补全的达人；rank 为全平台统一名次。"""

    platform: str
    kol_uid: str
    rank: int
    ranking_interaction: float


@dataclass(frozen=True)
class DetailEnrichmentPlan:
    """一份名单版本的 Top20 批量详情调用计划。"""

    selection_set_id: str
    groups: tuple[tuple[str, tuple[DetailEnrichmentTarget, ...]], ...]


def select_top20_detail_targets(items: Iterable[Any]) -> tuple[DetailEnrichmentTarget, ...]:
    """按近 30 天平均单帖互动量跨平台排序，稳定选取前 20 位。

    只有已经沉淀为白名单导出字段的 ``average_interactions`` 才参与排名，
    缺失或畸形数值不以 0 补造，避免把无数据达人误当作低互动达人。
    """
    candidates: list[tuple[float, str, str]] = []
    for item in items:
        platform = getattr(item, "platform", None)
        kol_uid = getattr(item, "kol_uid", None)
        if not isinstance(platform, str) or not platform.strip():
            continue
        if not isinstance(kol_uid, str) or not kol_uid.strip():
            continue
        interaction = _ranking_interaction(getattr(item, "fields_json", None))
        if interaction is None:
            continue
        candidates.append((interaction, platform, kol_uid))
    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    return tuple(
        DetailEnrichmentTarget(
            platform=platform,
            kol_uid=kol_uid,
            rank=index,
            ranking_interaction=interaction,
        )
        for index, (interaction, platform, kol_uid) in enumerate(candidates[:_MAX_TARGETS], start=1)
    )


def group_detail_targets_by_platform(
    targets: Iterable[DetailEnrichmentTarget],
) -> tuple[tuple[str, tuple[DetailEnrichmentTarget, ...]], ...]:
    """同平台 Top20 合为一次 ``kwUidList`` 调用，保持每个平台内的全局排名顺序。"""
    grouped: dict[str, list[DetailEnrichmentTarget]] = defaultdict(list)
    for target in targets:
        grouped[target.platform].append(target)
    return tuple(
        (platform, tuple(sorted(items, key=lambda item: item.rank)))
        for platform, items in sorted(grouped.items())
    )


def _ranking_interaction(fields: Any) -> float | None:
    if not isinstance(fields, dict):
        return None
    export_fields = fields.get("export_fields")
    if not isinstance(export_fields, dict):
        return None
    value = export_fields.get("average_interactions")
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip().replace(",", "")
    multiplier = Decimal(1)
    if text.endswith("万"):
        text = text[:-1]
        multiplier = Decimal(10_000)
    try:
        parsed = Decimal(text) * multiplier
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed < 0 or parsed > Decimal("1000000000000000"):
        return None
    return float(parsed)
