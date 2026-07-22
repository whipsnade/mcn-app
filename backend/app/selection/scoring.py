from __future__ import annotations

import math
from typing import Any, Mapping

from app.selection.schemas import CandidateScore, DimensionInputs, DimensionScore


SCORE_VERSION = "kol_score_v1"
WEIGHT_PROFILES: dict[str, dict[str, int]] = {
    "balanced": {
        "audience": 25,
        "content": 20,
        "engagement": 20,
        "budget": 15,
        "growth": 10,
        "brand_safety": 10,
    },
    "audience_first": {
        "audience": 35,
        "content": 20,
        "engagement": 15,
        "budget": 10,
        "growth": 10,
        "brand_safety": 10,
    },
    "performance_first": {
        "audience": 20,
        "content": 25,
        "engagement": 30,
        "budget": 10,
        "growth": 10,
        "brand_safety": 5,
    },
    "budget_first": {
        "audience": 20,
        "content": 15,
        "engagement": 15,
        "budget": 30,
        "growth": 10,
        "brand_safety": 10,
    },
    "risk_first": {
        "audience": 20,
        "content": 15,
        "engagement": 15,
        "budget": 10,
        "growth": 15,
        "brand_safety": 25,
    },
}


def score_candidate(dimensions: DimensionInputs, profile: str) -> CandidateScore:
    try:
        weights = WEIGHT_PROFILES[profile]
    except KeyError as exc:
        raise ValueError("unknown_scoring_profile") from exc
    values = dimensions.as_mapping()
    for value in values.values():
        if (
            value is not None
            and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
                or value > 100
            )
        ):
            raise ValueError("dimension_score_out_of_range")
    details = {
        name: DimensionScore(
            raw_score=value,
            weight=weights[name],
            weighted_score=0 if value is None else round(value * weights[name] / 100, 2),
        )
        for name, value in values.items()
    }
    completeness = sum(item.weight for item in details.values() if item.raw_score is not None)
    return CandidateScore(
        version=SCORE_VERSION,
        total=round(sum(item.weighted_score for item in details.values()), 2),
        weights=dict(weights),
        dimensions=details,
        data_completeness=completeness,
    )


def rating(total: float) -> tuple[str, str]:
    """总分对应的评级文字与星级（四档口径，供名单展示与聚合分桶）。"""
    if total >= 78:
        return "重点推荐", "★★★★★"
    if total >= 62:
        return "推荐", "★★★★"
    if total >= 48:
        return "可考虑", "★★★"
    return "观察", "★★"


def score_reason(fields: Mapping[str, Any]) -> str:
    """评分理由生成规则：Excel 导出与聚合分析共用同一口径。

    normalizers 白名单不产出 score_reason 字段，理由由代码按缺失字段情况生成：
    存在缺失字段时说明按评分规则处理，否则标注基于规范化 MCP 数据评分。
    """
    if fields.get("missing_fields"):
        return "数据缺失字段按评分规则处理"
    return "基于规范化 MCP 数据评分"
