"""KOL 价值评分 v3（kol_value_score_v3）纯计算器（spec §7.2 / Gate C Task 1）。

总分 100：效果与匹配度 70 + 价格效率 30。

- 效果维度权重固定（EFFECT_WEIGHTS_V3），缺失输入严格计 0、不重分配权重，
  缺失原因写入 missing_reason；
- 绝对量（互动、粉丝）按平台分别做异常值截断（winsorize）后 mid-rank
  percentile 归一；比例与匹配指标 clamp 0–100；
- 有效报价必须 >0 且匹配用户确认的 content_format；有效报价不足 3 个时所有
  价格效率分为 0；全部原始价格效率相同时统一 50 分位（15/30）；
- 报价缺失时价格效率 0 且排在有效报价达人之后；效果 <35/70 最高评级"观察"；
- preference 只改变排序主键（balanced=value / effect=effect / price=price），
  不改变评分公式；组内 platform + kol_uid 作为稳定 tie-breaker；
- 指标称"投放性价比指数"，不称 ROI。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

# 效果与匹配度八维权重（合计 70）。
EFFECT_WEIGHTS_V3 = {
    "average_interactions": 14,
    "active_follower": 10,
    "engagement_follower_ratio": 10,
    "content_match": 10,
    "followers": 7,
    "industry_interest": 7,
    "target_region": 6,
    "target_age": 6,
}
PRICE_WEIGHT = 30
EFFECT_TOTAL = 70
EFFECT_FLOOR = 35
# 有效报价样本下限：不足时价格效率分一律 0（避免极小样本制造高分）。
MIN_PRICE_SAMPLE = 3
# 绝对量异常值截断倍数（相对平台中位数）。
_OUTLIER_CAP_MULTIPLE = 10.0

Preference = Literal["effect", "balanced", "price"]
_RATING_BANDS = ((78, "重点推荐"), (62, "推荐"), (48, "可考虑"))


@dataclass(frozen=True)
class ScoreContextV3:
    """评分上下文：目标行业/地区/年龄与用户确认的内容形式。"""

    industry: str
    regions: tuple[str, ...]
    age_ranges: tuple[str, ...]
    content_formats: tuple[str, ...] = ()


@dataclass(frozen=True)
class CandidateInputV3:
    """单个候选的评分输入：绝对量 + 0–100 比例/匹配分 + 报价。"""

    platform: str
    kol_uid: str
    nickname: str
    average_interactions: float | None = None
    active_follower_rate: float | None = None
    engagement_follower_ratio: float | None = None
    content_match: float | None = None
    followers: int | None = None
    industry_interest: float | None = None
    target_region: float | None = None
    target_age: float | None = None
    quoted_price: float | None = None
    content_format: str | None = None


@dataclass(frozen=True)
class DimensionScoreV3:
    raw_score: float
    weight: int
    weighted_score: float
    source: str | None
    missing_reason: str | None


@dataclass(frozen=True)
class CandidateScoreV3:
    platform: str
    kol_uid: str
    nickname: str
    effect_score: float
    price_efficiency_score: float
    value_score: float
    quoted_price: float | None
    price_sample_size: int
    raw_price_efficiency: float | None
    price_efficiency_percentile: float | None
    rating: str
    data_completeness: float
    dimensions: dict[str, DimensionScoreV3]
    group: str
    rank: int


def _clamp(value: float | None) -> float | None:
    if value is None:
        return None
    return max(0.0, min(float(value), 100.0))


def _dimension(
    raw: float | None,
    weight: int,
    source: str,
    missing_reason: str,
) -> DimensionScoreV3:
    valid = raw is not None
    score = round(float(raw), 2) if valid else 0.0
    return DimensionScoreV3(
        raw_score=score,
        weight=weight,
        weighted_score=round(score * weight / 100, 2),
        source=source if valid else None,
        missing_reason=None if valid else missing_reason,
    )


def _mid_rank_percentile(pool: list[float], value: float) -> float:
    """mid-rank percentile（0–100）；全部相同 → 50；单值 → 50。

    先对池做异常值截断（winsorize 到中位数的固定倍数），候选值同样截断后再
    参与排名，保证极端值不会碾压同平台其余候选。
    """
    if not pool:
        return 0.0
    ordered = sorted(pool)
    median = ordered[len(ordered) // 2]
    cap = max(median * _OUTLIER_CAP_MULTIPLE, 1.0)
    winsorized = sorted(min(item, cap) for item in ordered)
    target = min(float(value), cap)
    n = len(winsorized)
    first = winsorized.index(target) + 1
    last = n - winsorized[::-1].index(target)
    mid = (first + last) / 2
    return round((mid - 0.5) / n * 100, 2)


def _platform_absolute_scores(
    candidates: tuple[CandidateInputV3, ...],
) -> tuple[dict[str, float], dict[str, float]]:
    """按平台分别收集绝对量池；返回 {稳定身份: 分位} 的互动与粉丝映射。"""
    pools_interactions: dict[str, list[float]] = {}
    pools_followers: dict[str, list[float]] = {}
    values_interactions: dict[str, float] = {}
    values_followers: dict[str, float] = {}
    for index, candidate in enumerate(candidates):
        key = f"{candidate.platform}\x00{index}"
        if candidate.average_interactions is not None and candidate.average_interactions >= 0:
            pools_interactions.setdefault(candidate.platform, []).append(float(candidate.average_interactions))
            values_interactions[key] = float(candidate.average_interactions)
        if candidate.followers is not None and candidate.followers >= 0:
            pools_followers.setdefault(candidate.platform, []).append(float(candidate.followers))
            values_followers[key] = float(candidate.followers)
    return (
        {
            key: _mid_rank_percentile(pools_interactions[candidate.platform], value)
            for key, value in values_interactions.items()
        },
        {
            key: _mid_rank_percentile(pools_followers[candidate.platform], value)
            for key, value in values_followers.items()
        },
    )


def _normalize_format(value: str | None) -> str:
    if not value:
        return ""
    return "".join(ch for ch in value.strip().casefold() if ch.isalnum())


def _is_valid_quote(candidate: CandidateInputV3, context: ScoreContextV3) -> bool:
    if candidate.quoted_price is None or candidate.quoted_price <= 0:
        return False
    if not context.content_formats:
        return True
    confirmed = {_normalize_format(item) for item in context.content_formats if item}
    if not confirmed:
        return True
    return _normalize_format(candidate.content_format) in confirmed


def _rating_from_scores(effect_score: float, value_score: float) -> str:
    """评级：重点推荐 ≥78 / 推荐 62–77 / 可考虑 48–61 / 观察 <48 或效果未过底线。"""
    if effect_score < EFFECT_FLOOR:
        return "观察"
    for threshold, rating in _RATING_BANDS:
        if value_score >= threshold:
            return rating
    return "观察"


def score_and_rank_candidates_v3(
    context: ScoreContextV3,
    candidates: Sequence[CandidateInputV3],
    preference: Preference,
) -> tuple[CandidateScoreV3, ...]:
    """对候选池评分并排序；返回带 rank 的评分结果（默认整池排序，Top20 由调用方截取）。"""
    pool = tuple(candidates)
    if not pool:
        return ()

    interactions_percentile, followers_percentile = _platform_absolute_scores(pool)

    # 1. 效果分（0–70）。
    scored: list[dict[str, object]] = []
    valid_quotes: list[float] = []
    raw_by_key: dict[str, float] = {}
    for index, candidate in enumerate(pool):
        key = f"{candidate.platform}\x00{index}"
        dimensions = {
            "average_interactions": _dimension(
                interactions_percentile.get(key), 14, "average_interactions",
                "missing_average_interactions",
            ),
            "active_follower": _dimension(
                _clamp(candidate.active_follower_rate), 10, "active_follower_rate",
                "missing_active_follower",
            ),
            "engagement_follower_ratio": _dimension(
                _clamp(candidate.engagement_follower_ratio), 10,
                "engagement_follower_ratio", "missing_engagement_follower_ratio",
            ),
            "content_match": _dimension(
                _clamp(candidate.content_match), 10, "content_match", "missing_content_match",
            ),
            "followers": _dimension(
                followers_percentile.get(key), 7, "followers", "missing_followers",
            ),
            "industry_interest": _dimension(
                _clamp(candidate.industry_interest), 7, "industry_interest",
                "missing_industry_interest",
            ),
            "target_region": _dimension(
                _clamp(candidate.target_region), 6, "target_region", "missing_target_region",
            ),
            "target_age": _dimension(
                _clamp(candidate.target_age), 6, "target_age", "missing_target_age",
            ),
        }
        effect_score = round(
            sum(item.weighted_score for item in dimensions.values()), 2
        )
        is_valid_quote = _is_valid_quote(candidate, context)
        if is_valid_quote:
            quote = float(candidate.quoted_price)  # type: ignore[arg-type]
            valid_quotes.append(quote)
            raw_by_key[key] = effect_score / quote
        scored.append(
            {
                "key": key,
                "candidate": candidate,
                "dimensions": dimensions,
                "effect_score": effect_score,
                "is_valid_quote": is_valid_quote,
            }
        )

    # 2. 价格效率（0–30）：有效样本 ≥3 才对 effect/quote 做全候选 mid-rank 分位。
    price_sample_size = len(valid_quotes)
    price_pool: dict[str, float] = {}
    if price_sample_size >= MIN_PRICE_SAMPLE:
        price_pool = {key: _mid_rank_percentile(list(raw_by_key.values()), raw) for key, raw in raw_by_key.items()}

    # 3. 分组与排序（preference 只改主键；platform+kol_uid 稳定 tie-breaker）。
    groups = {"priced_qualified": 0, "unpriced_qualified": 1, "below_floor": 2}
    entries: list[tuple[int, float, str, str, CandidateScoreV3]] = []
    for entry in scored:
        candidate = entry["candidate"]
        effect_score = float(entry["effect_score"])
        if entry["is_valid_quote"]:
            percentile = price_pool.get(entry["key"])
            price_score = round(percentile * PRICE_WEIGHT / 100, 2) if percentile is not None else 0.0
            # 样本不足时不暴露原始价格效率与分位（避免误导）。
            raw_efficiency = raw_by_key.get(entry["key"]) if price_pool else None
        else:
            percentile = None
            price_score = 0.0
            raw_efficiency = None
        value_score = round(effect_score + price_score, 2)
        if effect_score < EFFECT_FLOOR:
            group = "below_floor"
        elif entry["is_valid_quote"]:
            group = "priced_qualified"
        else:
            group = "unpriced_qualified"
        present_weights = sum(
            item.weight for item in entry["dimensions"].values() if item.missing_reason is None
        )
        data_completeness = round(present_weights / EFFECT_TOTAL * 100, 2)
        result = CandidateScoreV3(
            platform=candidate.platform,
            kol_uid=candidate.kol_uid,
            nickname=candidate.nickname,
            effect_score=effect_score,
            price_efficiency_score=price_score,
            value_score=value_score,
            quoted_price=candidate.quoted_price if entry["is_valid_quote"] else None,
            price_sample_size=price_sample_size,
            raw_price_efficiency=raw_efficiency,
            price_efficiency_percentile=percentile,
            rating=_rating_from_scores(effect_score, value_score),
            data_completeness=data_completeness,
            dimensions=dict(entry["dimensions"]),
            group=group,
            rank=0,
        )
        if preference == "effect":
            primary = effect_score
        elif preference == "price":
            primary = price_score
        else:
            primary = value_score
        entries.append((groups[group], -primary, candidate.platform, candidate.kol_uid, result))

    entries.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    return tuple(
        CandidateScoreV3(
            platform=result.platform,
            kol_uid=result.kol_uid,
            nickname=result.nickname,
            effect_score=result.effect_score,
            price_efficiency_score=result.price_efficiency_score,
            value_score=result.value_score,
            quoted_price=result.quoted_price,
            price_sample_size=result.price_sample_size,
            raw_price_efficiency=result.raw_price_efficiency,
            price_efficiency_percentile=result.price_efficiency_percentile,
            rating=result.rating,
            data_completeness=result.data_completeness,
            dimensions=result.dimensions,
            group=result.group,
            rank=index,
        )
        for index, (_g, _p, _plat, _uid, result) in enumerate(entries, start=1)
    )


__all__ = [
    "CandidateInputV3",
    "CandidateScoreV3",
    "DimensionScoreV3",
    "EFFECT_WEIGHTS_V3",
    "EFFECT_FLOOR",
    "ScoreContextV3",
    "score_and_rank_candidates_v3",
]
