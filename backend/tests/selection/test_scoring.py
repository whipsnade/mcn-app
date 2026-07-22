import pytest

from app.selection.scoring import WEIGHT_PROFILES, score_candidate, score_reason
from tests.selection.fakes import all_dimensions


def test_balanced_profile_uses_confirmed_weights() -> None:
    result = score_candidate(all_dimensions(80), profile="balanced")

    assert result.total == 80
    assert result.weights == {
        "audience": 25,
        "content": 20,
        "engagement": 20,
        "budget": 15,
        "growth": 10,
        "brand_safety": 10,
    }


def test_every_weight_profile_totals_one_hundred() -> None:
    assert {sum(weights.values()) for weights in WEIGHT_PROFILES.values()} == {100}


def test_missing_dimension_is_not_fabricated_as_observed_zero() -> None:
    result = score_candidate(all_dimensions(80, engagement=None), profile="balanced")

    assert result.dimensions["engagement"].raw_score is None
    assert result.dimensions["engagement"].weighted_score == 0
    assert result.data_completeness == 80


def test_unknown_profile_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown_scoring_profile"):
        score_candidate(all_dimensions(80), profile="unsupported")


def test_out_of_range_dimension_score_is_rejected() -> None:
    with pytest.raises(ValueError, match="dimension_score_out_of_range"):
        score_candidate(all_dimensions(101), profile="balanced")


def test_rating_boundaries_match_four_tier_spec() -> None:
    from app.selection.scoring import rating

    assert rating(78) == ("重点推荐", "★★★★★")
    assert rating(62) == ("推荐", "★★★★")
    assert rating(48) == ("可考虑", "★★★")
    assert rating(47.9) == ("观察", "★★")
    assert rating(0) == ("观察", "★★")


class TestScoreReason:
    """导出 Excel 与聚合分析共用的评分理由生成规则（exporter/analysis 同一口径）。"""

    def test_missing_fields_present(self) -> None:
        assert score_reason({"missing_fields": ["engagement_rate"]}) == (
            "数据缺失字段按评分规则处理"
        )

    def test_no_missing_fields(self) -> None:
        assert score_reason({"missing_fields": []}) == "基于规范化 MCP 数据评分"

    def test_missing_fields_key_absent(self) -> None:
        assert score_reason({}) == "基于规范化 MCP 数据评分"
