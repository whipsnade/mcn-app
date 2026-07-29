"""KOL 圈选八维严格评分（v2）的纯计算器。"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import Any


SCORE_VERSION_V2 = "kol_score_v2"
WEIGHTS_V2 = {
    "industry_interest": 10,
    "target_region": 8,
    "target_age": 8,
    "engagement": 20,
    "active_follower": 15,
    "content": 15,
    "followers": 10,
    "engagement_follower_ratio": 14,
}


@dataclass(frozen=True)
class ScoreContextV2:
    industry: str
    regions: tuple[str, ...]
    age_ranges: tuple[str, ...]


@dataclass(frozen=True)
class ScoreInputsV2:
    audience_interests: dict[str, float] = field(default_factory=dict)
    audience_regions: dict[str, float] = field(default_factory=dict)
    audience_age: dict[str, float] = field(default_factory=dict)
    average_interactions: float | None = None
    effective_follower_rate: float | None = None
    active_follower_count: int | None = None
    content_score: float | None = None
    followers: int | None = None
    interaction_follower_ratio: float | None = None


def _text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value.strip())
    return normalized.casefold().replace(" ", "").replace("岁", "").replace("至", "-")


def _distribution_score(values: dict[str, float], targets: tuple[str, ...]) -> float | None:
    if not values or not targets:
        return None
    target_set = {_text(item) for item in targets if item.strip()}
    if not target_set:
        return None
    return min(100.0, sum(float(value) for key, value in values.items() if _text(key) in target_set))


def _bucket(value: float | None, boundaries: tuple[float, float, float, float]) -> float | None:
    if value is None or value < 0:
        return None
    for score, boundary in zip((20.0, 40.0, 60.0, 80.0), boundaries, strict=True):
        if value < boundary:
            return score
    return 100.0


def _dimension(raw: float | None, weight: int, source: str, missing_reason: str) -> dict[str, Any]:
    valid = raw is not None and 0 <= raw <= 100
    score = round(float(raw), 2) if valid else 0.0
    return {
        "raw_score": score,
        "weight": weight,
        "weighted_score": round(score * weight / 100, 2),
        "source": source if valid else None,
        "missing_reason": None if valid else missing_reason,
    }


def _rating(total: float) -> tuple[str, str]:
    if total >= 78:
        return "重点推荐", "★★★★★"
    if total >= 62:
        return "推荐", "★★★★"
    if total >= 48:
        return "可考虑", "★★★"
    return "观察", "★★"


def score_candidate_v2(context: ScoreContextV2, inputs: ScoreInputsV2) -> dict[str, Any]:
    ratio = inputs.interaction_follower_ratio
    if ratio is None and inputs.average_interactions is not None and inputs.followers:
        ratio = inputs.average_interactions / inputs.followers * 100
    active = inputs.effective_follower_rate
    if active is None and inputs.active_follower_count is not None and inputs.followers:
        active = inputs.active_follower_count / inputs.followers * 100
    dimensions = {
        "industry_interest": _dimension(
            _distribution_score(inputs.audience_interests, (context.industry,)), 10,
            "audience_interests", "missing_industry_interest",
        ),
        "target_region": _dimension(
            _distribution_score(inputs.audience_regions, context.regions), 8,
            "audience_regions", "missing_target_region",
        ),
        "target_age": _dimension(
            _distribution_score(inputs.audience_age, context.age_ranges), 8,
            "audience_age", "missing_target_age",
        ),
        "engagement": _dimension(
            _bucket(inputs.average_interactions, (1_000, 5_000, 20_000, 100_000)), 20,
            "average_interactions", "missing_average_interactions",
        ),
        "active_follower": _dimension(active, 15, "effective_follower_rate", "missing_active_follower"),
        "content": _dimension(inputs.content_score, 15, "content_score", "missing_content_score"),
        "followers": _dimension(
            _bucket(float(inputs.followers) if inputs.followers is not None else None, (10_000, 100_000, 500_000, 1_000_000)),
            10, "followers", "missing_followers",
        ),
        "engagement_follower_ratio": _dimension(
            _bucket(ratio, (0.5, 1.0, 3.0, 6.0)), 14,
            "interaction_follower_ratio", "missing_interaction_follower_ratio",
        ),
    }
    total = round(sum(item["weighted_score"] for item in dimensions.values()), 2)
    rating, stars = _rating(total)
    return {
        "version": SCORE_VERSION_V2,
        "total": total,
        "weights": dict(WEIGHTS_V2),
        "dimensions": dimensions,
        "data_completeness": sum(
            item["weight"] for item in dimensions.values() if item["missing_reason"] is None
        ),
        "rating": rating,
        "stars": stars,
    }
