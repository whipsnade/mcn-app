"""kol_selection_v3: 圈选达人强类型 payload (spec §12.1).

The scoring block is the strictest contract: `scoring.version="kol_score_v2"`,
`method="weighted_sum"`, weights exactly the eight-dimension set summing to 100,
and every `items[].score_snapshot` must freeze total/rating/stars/
data_completeness plus all eight dimensions with every sub-field.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent_artifacts.payloads.common import (
    ArtifactPayloadBase,
    DistributionItem,
    OptionalHttpUrl,
    UniqueKeyValidator,
)

SCORE_DIMENSIONS = (
    "industry_interest",
    "target_region",
    "target_age",
    "engagement",
    "active_follower",
    "content",
    "followers",
    "engagement_follower_ratio",
)
WEIGHTS = {
    "industry_interest": 10,
    "target_region": 8,
    "target_age": 8,
    "engagement": 20,
    "active_follower": 15,
    "content": 15,
    "followers": 10,
    "engagement_follower_ratio": 14,
}


class AudienceFilter(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    regions: tuple[str, ...] = Field(default_factory=tuple)
    age_ranges: tuple[str, ...] = Field(default_factory=tuple)
    interests: tuple[str, ...] = Field(default_factory=tuple)


class SelectionFilters(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    budget_min: int | None = None
    budget_max: int | None = None
    follower_min: int | None = None
    follower_max: int | None = None


class KolSelectionScope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    brand: str | None = None
    category: str | None = None
    campaign: str | None = None
    platforms: tuple[str, ...] = Field(default_factory=tuple)
    audience: AudienceFilter
    filters: SelectionFilters


class ScoringConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal["kol_score_v2"]
    method: Literal["weighted_sum"]
    weights: dict[str, int]
    missing_value_policy: Literal["missing_as_zero"]

    @model_validator(mode="after")
    def _validate_weights(self) -> ScoringConfig:
        if dict(self.weights) != WEIGHTS:
            raise ValueError(
                "weights must match exactly the eight kol_score_v2 dimensions "
                "summing to 100"
            )
        return self


class ScoreDimension(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    raw_score: float
    weight: int
    weighted_score: float
    source: str | None
    missing_reason: str | None


class ScoreSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal["kol_score_v2"]
    total: float
    rating: str
    stars: str
    data_completeness: float
    dimensions: dict[str, ScoreDimension]

    @model_validator(mode="after")
    def _validate_dimensions(self) -> ScoreSnapshot:
        if set(self.dimensions) != set(SCORE_DIMENSIONS):
            raise ValueError(
                "score_snapshot dimensions must contain exactly the eight "
                "kol_score_v2 dimensions"
            )
        return self


class KolSelectionItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rank: int
    platform: str
    kol_uid: str
    nickname: str
    avatar_url: OptionalHttpUrl = None
    homepage_url: OptionalHttpUrl = None
    followers: int | None
    active_followers: int | None
    active_follower_rate: float | None
    growth_rate: float | None
    engagement_total: int | None
    avg_engagement: float | None
    likes: int | None
    comments: int | None
    shares: int | None
    quoted_price: int | None = None
    reasons: tuple[str, ...] = Field(default_factory=tuple)
    missing_fields: tuple[str, ...] = Field(default_factory=tuple)
    audience: AudienceFilter
    score_snapshot: ScoreSnapshot


class KolSelectionSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_count: int | None
    selected_count: int | None
    platform_distribution: tuple[DistributionItem, ...] = Field(default_factory=tuple)
    rating_distribution: tuple[DistributionItem, ...] = Field(default_factory=tuple)


class KolSelectionData(UniqueKeyValidator):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scoring: ScoringConfig
    items: tuple[KolSelectionItem, ...] = Field(default_factory=tuple, max_length=20)
    summary: KolSelectionSummary

    STABLE_KEYS = {
        "items": ("platform", "kol_uid"),
    }


class KolSelectionNote(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    kol_uid: str | None = None
    supporting_paths: tuple[str, ...] = Field(default_factory=tuple)


class KolSelectionNarrative(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    selection_summary: str
    fit_findings: tuple[KolSelectionNote, ...] = Field(default_factory=tuple)
    risk_notes: tuple[KolSelectionNote, ...] = Field(default_factory=tuple)
    usage_advice: tuple[KolSelectionNote, ...] = Field(default_factory=tuple)


class KolSelectionV3(ArtifactPayloadBase):
    schema_version: Literal["kol_selection_v3"] = "kol_selection_v3"
    module: Literal["kol"] = "kol"

    scope: KolSelectionScope
    data: KolSelectionData
    narrative: KolSelectionNarrative

    REQUIRED_SECTIONS = frozenset({"scoring", "items", "summary"})
    SECTION_NUMERIC_PATHS = {
        "summary": ("summary.candidate_count", "summary.selected_count"),
    }
