from __future__ import annotations

from app.reporting.schemas import CandidateScore, DimensionInputs, DimensionScore


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
    details = {
        name: DimensionScore(
            raw_score=value,
            weight=weights[name],
            weighted_score=0 if value is None else round(value * weights[name] / 100, 2),
        )
        for name, value in dimensions.as_mapping().items()
    }
    completeness = sum(item.weight for item in details.values() if item.raw_score is not None)
    return CandidateScore(
        version=SCORE_VERSION,
        total=round(sum(item.weighted_score for item in details.values()), 2),
        weights=dict(weights),
        dimensions=details,
        data_completeness=completeness,
    )
