from __future__ import annotations

import pytest

from app.selection.scoring_v2 import ScoreContextV2, ScoreInputsV2, score_candidate_v2


def _context() -> ScoreContextV2:
    return ScoreContextV2(
        industry="美食", regions=("上海", "杭州"), age_ranges=("18-24", "25-34")
    )


def test_scores_all_eight_dimensions_with_effect_first_weights() -> None:
    result = score_candidate_v2(
        _context(),
        ScoreInputsV2(
            audience_interests={"美食": 80}, audience_regions={"上海": 50},
            audience_age={"18-24岁": 40, "25至34": 30}, average_interactions=20_000,
            effective_follower_rate=60, content_score=90, followers=500_000,
        ),
    )

    assert result["version"] == "kol_score_v2"
    assert result["weights"] == {
        "industry_interest": 10, "target_region": 8, "target_age": 8,
        "engagement": 20, "active_follower": 15, "content": 15,
        "followers": 10, "engagement_follower_ratio": 14,
    }
    assert result["dimensions"]["target_age"]["raw_score"] == 70
    assert result["dimensions"]["engagement"]["raw_score"] == 80
    assert result["dimensions"]["followers"]["raw_score"] == 80
    assert result["dimensions"]["engagement_follower_ratio"]["raw_score"] == 80
    assert result["total"] == 75.3
    assert result["rating"] == "推荐"
    assert result["data_completeness"] == 100


@pytest.mark.parametrize(
    ("followers", "interactions", "ratio", "expected"),
    [
        (9_999, 999, 0.49, (20, 20, 20)),
        (10_000, 1_000, 0.5, (40, 40, 40)),
        (100_000, 5_000, 1.0, (60, 60, 60)),
        (500_000, 20_000, 3.0, (80, 80, 80)),
        (1_000_000, 100_000, 6.0, (100, 100, 100)),
    ],
)
def test_bucket_boundaries_are_left_closed(
    followers: int, interactions: int, ratio: float, expected: tuple[int, int, int]
) -> None:
    result = score_candidate_v2(
        ScoreContextV2(industry="美食", regions=("上海",), age_ranges=("18-24",)),
        ScoreInputsV2(
            followers=followers, average_interactions=interactions,
            interaction_follower_ratio=ratio,
        ),
    )
    dimensions = result["dimensions"]
    assert (
        dimensions["followers"]["raw_score"], dimensions["engagement"]["raw_score"],
        dimensions["engagement_follower_ratio"]["raw_score"],
    ) == expected


def test_missing_input_is_zero_without_weight_redistribution() -> None:
    result = score_candidate_v2(_context(), ScoreInputsV2(content_score=80))

    assert result["dimensions"]["content"]["weighted_score"] == 12
    assert result["dimensions"]["engagement"]["raw_score"] == 0
    assert result["dimensions"]["engagement"]["missing_reason"] == "missing_average_interactions"
    assert result["total"] == 12
    assert result["data_completeness"] == 15


def test_full_width_age_bucket_matches_after_normalization() -> None:
    result = score_candidate_v2(
        ScoreContextV2(industry="美食", regions=("上海",), age_ranges=("18-24",)),
        ScoreInputsV2(audience_age={"１８－２４岁": 55}),
    )

    assert result["dimensions"]["target_age"]["raw_score"] == 55
