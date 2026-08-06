"""kol_selection_v3: 圈选达人强类型 payload (spec §12.1 / Gate C §7.2).

The scoring block is a strict contract. ``score_snapshot`` is a discriminated
union keyed on ``version``: historical ``kol_score_v2`` stays readable, new
builds produce ``kol_value_score_v3`` (effect 70 + price efficiency 30).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent_artifacts.payloads.common import (
    ArtifactPayloadBase,
    DistributionItem,
    OptionalHttpUrl,
    UniqueKeyValidator,
)
from app.selection.scoring_v2 import WEIGHTS_V2
from app.selection.scoring_v3 import EFFECT_WEIGHTS_V3

# kol_score_v2 八维权重/维度的单一事实来源是 selection/scoring_v2.WEIGHTS_V2
# （评分器唯一真源）。payload 派生自它，避免双表漂移导致「评分块声称的权重」
# 与实际评分使用的权重不一致。
SCORE_DIMENSIONS = tuple(WEIGHTS_V2)
WEIGHTS = dict(WEIGHTS_V2)
# kol_value_score_v3 的效果维度权重/维度单一事实来源是 scoring_v3.EFFECT_WEIGHTS_V3。
V3_DIMENSIONS = tuple(EFFECT_WEIGHTS_V3)
V3_WEIGHTS = dict(EFFECT_WEIGHTS_V3)


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
    # 用户确认的内容形式（报价有效性约束；缺省不限制）。
    content_formats: tuple[str, ...] = Field(default_factory=tuple)


class ScoringConfigV2(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal["kol_score_v2"]
    method: Literal["weighted_sum"]
    weights: dict[str, int]
    missing_value_policy: Literal["missing_as_zero"]

    @model_validator(mode="after")
    def _validate_weights(self) -> ScoringConfigV2:
        if dict(self.weights) != WEIGHTS:
            raise ValueError(
                "weights must match exactly the eight kol_score_v2 dimensions "
                "summing to 100"
            )
        return self


class ScoringConfigV3(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal["kol_value_score_v3"]
    method: Literal["effect_plus_price_efficiency"]
    weights: dict[str, int]
    missing_value_policy: Literal["missing_as_zero"]

    @model_validator(mode="after")
    def _validate_weights(self) -> ScoringConfigV3:
        if dict(self.weights) != V3_WEIGHTS:
            raise ValueError(
                "weights must match exactly the eight kol_value_score_v3 effect "
                "dimensions summing to 70"
            )
        return self


ScoringConfig = Annotated[
    ScoringConfigV2 | ScoringConfigV3,
    Field(discriminator="version"),
]


class ScoreDimension(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    raw_score: float
    weight: int
    weighted_score: float
    source: str | None
    missing_reason: str | None


class KolScoreSnapshotV2(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal["kol_score_v2"]
    total: float
    rating: str
    stars: str
    data_completeness: float
    dimensions: dict[str, ScoreDimension]

    @model_validator(mode="after")
    def _validate_dimensions(self) -> KolScoreSnapshotV2:
        if set(self.dimensions) != set(SCORE_DIMENSIONS):
            raise ValueError(
                "score_snapshot dimensions must contain exactly the eight "
                "kol_score_v2 dimensions"
            )
        return self


class KolValueScoreSnapshotV3(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal["kol_value_score_v3"]
    effect_score: float
    price_efficiency_score: float
    value_score: float
    quoted_price: float | None
    price_sample_size: int
    raw_price_efficiency: float | None
    price_efficiency_percentile: float | None
    rating: str
    data_completeness: float
    dimensions: dict[str, ScoreDimension]

    @model_validator(mode="after")
    def _validate_dimensions(self) -> KolValueScoreSnapshotV3:
        if set(self.dimensions) != set(V3_DIMENSIONS):
            raise ValueError(
                "score_snapshot dimensions must contain exactly the eight "
                "kol_value_score_v3 effect dimensions"
            )
        for name, dimension in self.dimensions.items():
            if dimension.weight != V3_WEIGHTS[name]:
                raise ValueError(f"dimension {name} weight must be {V3_WEIGHTS[name]}")
        return self


ScoreSnapshot = Annotated[
    KolScoreSnapshotV2 | KolValueScoreSnapshotV3,
    Field(discriminator="version"),
]


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
    GOVERNED_SECTIONS = frozenset({"scoring", "items", "summary"})
