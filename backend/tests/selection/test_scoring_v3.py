"""kol_value_score_v3 纯计算器测试（spec §7.2 / Gate C Task 1）。

效果与匹配度 70 分、价格效率 30 分；缺失严格计 0 不重分配；绝对量按平台
mid-rank percentile + 异常值截断；有效报价 ≥3 才计算价格效率；全部相同取
50 分位（15/30）；报价缺失置后；效果 <35/70 最高"观察"；preference 只改排序主键。
"""

from __future__ import annotations

import pytest

from app.selection.scoring_v3 import (
    EFFECT_WEIGHTS_V3,
    CandidateInputV3,
    ScoreContextV3,
    score_and_rank_candidates_v3,
)

CONTEXT = ScoreContextV3(
    industry="美食",
    regions=("上海",),
    age_ranges=("18-24",),
    content_formats=("视频", "图文"),
)


def _candidate(
    *,
    platform: str = "xiaohongshu",
    kol_uid: str = "k1",
    nickname: str = "达人",
    interactions: float | None = 10_000.0,
    active: float | None = 80.0,
    ratio: float | None = 5.0,
    content: float | None = 90.0,
    followers: int | None = 1_000_000,
    industry: float | None = 90.0,
    region: float | None = 90.0,
    age: float | None = 90.0,
    price: float | None = None,
    content_format: str = "视频",
) -> CandidateInputV3:
    return CandidateInputV3(
        platform=platform,
        kol_uid=kol_uid,
        nickname=nickname,
        average_interactions=interactions,
        active_follower_rate=active,
        engagement_follower_ratio=ratio,
        content_match=content,
        followers=followers,
        industry_interest=industry,
        target_region=region,
        target_age=age,
        quoted_price=price,
        content_format=content_format,
    )


def _effect_pool() -> list[CandidateInputV3]:
    """三个候选：d1 效果最高、d2 次之、d3 最低（绝对量跨平台分位区分）。"""
    return [
        _candidate(kol_uid="d1", interactions=50_000.0, followers=2_000_000, price=5_000),
        _candidate(kol_uid="d2", interactions=20_000.0, followers=1_000_000, price=3_000),
        _candidate(kol_uid="d3", interactions=2_000.0, followers=100_000, price=1_000),
    ]


def test_v3_weights_sum_to_70() -> None:
    assert sum(EFFECT_WEIGHTS_V3.values()) == 70
    assert set(EFFECT_WEIGHTS_V3) == {
        "average_interactions",
        "active_follower",
        "engagement_follower_ratio",
        "content_match",
        "followers",
        "industry_interest",
        "target_region",
        "target_age",
    }


def test_v3_missing_dimension_is_zero_with_reason() -> None:
    candidate = _candidate(age=None, interactions=None, followers=None)
    (result,) = score_and_rank_candidates_v3(CONTEXT, [candidate], "balanced")
    assert result.dimensions["target_age"].raw_score == 0
    assert result.dimensions["target_age"].missing_reason == "missing_target_age"
    assert result.dimensions["average_interactions"].missing_reason == "missing_average_interactions"
    assert result.dimensions["followers"].missing_reason == "missing_followers"
    assert sum(item.weight for item in result.dimensions.values()) == 70
    # 缺失 6+7+14 分权重：效果分严格扣减，不重分配。
    assert result.effect_score <= 70 - 27 + 1e-6


def test_v3_effect_absolute_values_use_platform_percentile() -> None:
    rows = score_and_rank_candidates_v3(CONTEXT, _effect_pool(), "balanced")
    by_uid = {row.kol_uid: row for row in rows}
    # d1 互动/粉丝最多 → 该维度分位最高（同一平台池）。
    assert by_uid["d1"].dimensions["average_interactions"].raw_score > by_uid["d3"].dimensions["average_interactions"].raw_score
    assert by_uid["d1"].dimensions["followers"].raw_score > by_uid["d3"].dimensions["followers"].raw_score


def test_v3_ratios_and_match_clamped_to_100() -> None:
    candidate = _candidate(ratio=500.0, content=120.0, industry=-5.0, active=300.0)
    (result,) = score_and_rank_candidates_v3(CONTEXT, [candidate], "balanced")
    assert result.dimensions["engagement_follower_ratio"].raw_score <= 100
    assert result.dimensions["content_match"].raw_score <= 100
    assert result.dimensions["industry_interest"].raw_score >= 0
    assert result.dimensions["active_follower"].raw_score <= 100


def test_v3_price_score_requires_three_valid_quotes() -> None:
    rows = score_and_rank_candidates_v3(
        CONTEXT, [_candidate(price=1000), _candidate(kol_uid="k2", price=2000)], "balanced"
    )
    assert {row.price_efficiency_score for row in rows} == {0}
    assert all(row.price_sample_size == 2 for row in rows)
    assert all(row.raw_price_efficiency is None for row in rows)


def test_v3_price_requires_positive_quote_and_format_match() -> None:
    pool = [
        _candidate(kol_uid="ok", price=5000, content_format="视频"),
        _candidate(kol_uid="bad0", price=0, content_format="视频"),
        _candidate(kol_uid="badneg", price=-100, content_format="视频"),
        _candidate(kol_uid="badfmt", price=5000, content_format="直播"),
    ]
    rows = {row.kol_uid: row for row in score_and_rank_candidates_v3(CONTEXT, pool, "balanced")}
    # 有效报价只有 1 个（<3）→ 全部价格分 0，样本数记录 1。
    assert rows["ok"].price_sample_size == 1
    assert all(row.price_efficiency_score == 0 for row in rows.values())


def test_v3_all_same_price_efficiency_gets_mid_15() -> None:
    pool = [
        _candidate(kol_uid="a", price=1000, interactions=10_000, followers=1_000_000),
        _candidate(kol_uid="b", price=1000, interactions=10_000, followers=1_000_000),
        _candidate(kol_uid="c", price=1000, interactions=10_000, followers=1_000_000),
    ]
    rows = score_and_rank_candidates_v3(CONTEXT, pool, "balanced")
    assert all(row.price_efficiency_score == pytest.approx(15.0) for row in rows)
    assert all(row.price_efficiency_percentile == pytest.approx(50.0) for row in rows)


def test_v3_missing_price_ranked_after_priced() -> None:
    pool = [
        _candidate(kol_uid="unpriced", price=None, interactions=50_000, followers=2_000_000),
        _candidate(kol_uid="p1", price=5_000, interactions=40_000, followers=1_500_000),
        _candidate(kol_uid="p2", price=6_000, interactions=30_000, followers=1_200_000),
        _candidate(kol_uid="p3", price=7_000, interactions=20_000, followers=1_000_000),
    ]
    rows = score_and_rank_candidates_v3(CONTEXT, pool, "balanced")
    assert rows[0].quoted_price is not None
    assert rows[1].quoted_price is not None
    assert rows[2].quoted_price is not None
    assert rows[3].kol_uid == "unpriced"
    assert rows[3].price_efficiency_score == 0


def test_v3_effect_floor_caps_rating_at_observation() -> None:
    pool = [
        _candidate(kol_uid="low", interactions=None, followers=None, active=None, ratio=None, content=None, industry=None, region=None, age=None),
        _candidate(kol_uid="ok", interactions=50_000, followers=2_000_000, price=5_000),
    ]
    rows = score_and_rank_candidates_v3(CONTEXT, pool, "balanced")
    by_uid = {row.kol_uid: row for row in rows}
    assert by_uid["low"].effect_score < 35
    assert by_uid["low"].rating == "观察"
    assert by_uid["low"].group == "below_floor"


def test_v3_rating_bands() -> None:
    assert _rating_for(effect=60, value=90) == "重点推荐"  # 90
    assert _rating_for(effect=50, value=70) == "推荐"  # 70
    assert _rating_for(effect=40, value=55) == "可考虑"  # 55
    assert _rating_for(effect=30, value=40) == "观察"  # 40
    assert _rating_for(effect=34, value=64) == "观察"  # 64 但效果未过底线


def _rating_for(effect: float, value: float) -> str:
    """直接构造评级（供 band 边界测试）。"""
    from app.selection.scoring_v3 import _rating_from_scores

    return _rating_from_scores(effect, value)


def test_v3_preference_only_changes_order_not_scores() -> None:
    pool = _effect_pool()
    balanced = score_and_rank_candidates_v3(CONTEXT, pool, "balanced")
    effect = score_and_rank_candidates_v3(CONTEXT, pool, "effect")
    price = score_and_rank_candidates_v3(CONTEXT, pool, "price")
    scores_by_uid = {row.kol_uid: (row.effect_score, row.price_efficiency_score, row.value_score) for row in balanced}
    for rows in (balanced, effect, price):
        for row in rows:
            assert (row.effect_score, row.price_efficiency_score, row.value_score) == scores_by_uid[row.kol_uid]
    # balanced 按价值分、effect 按效果分、price 按价格效率分排序。
    assert [row.kol_uid for row in balanced] == sorted(
        (row.kol_uid for row in balanced),
        key=lambda uid: -scores_by_uid[uid][2],
    )
    assert [row.kol_uid for row in effect] == sorted(
        (row.kol_uid for row in effect),
        key=lambda uid: -scores_by_uid[uid][0],
    )
    assert [row.kol_uid for row in price] == sorted(
        (row.kol_uid for row in price),
        key=lambda uid: -scores_by_uid[uid][1],
    )


def test_v3_stable_tie_breaker_platform_then_kol_uid() -> None:
    pool = [
        _candidate(platform="xiaohongshu", kol_uid="b"),
        _candidate(platform="xiaohongshu", kol_uid="a"),
        _candidate(platform="douyin", kol_uid="z"),
        _candidate(platform="douyin", kol_uid="y"),
    ]
    first = score_and_rank_candidates_v3(CONTEXT, pool, "balanced")
    second = score_and_rank_candidates_v3(CONTEXT, list(reversed(pool)), "balanced")
    assert [(row.platform, row.kol_uid) for row in first] == [(row.platform, row.kol_uid) for row in second]


def test_v3_rank_is_assigned_in_order() -> None:
    rows = score_and_rank_candidates_v3(CONTEXT, _effect_pool(), "balanced")
    assert [row.rank for row in rows] == [1, 2, 3]
