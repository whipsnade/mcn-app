"""kol_analysis_v2: KOL 分析强类型 payload (spec §12.1)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agent_artifacts.payloads.common import (
    ArtifactPayloadBase,
    DistributionItem,
    NarrativeFinding,
    UniqueKeyValidator,
)


class KolAnalysisScope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    selection_artifact_id: str
    selection_version: str
    analysis_period: str | None = None


class KolAnalysisSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kol_count: int | None
    total_followers: int | None
    total_active_followers: int | None
    total_engagement: int | None
    avg_score: float | None


class KolTrendItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    platform: str
    kol_uid: str
    nickname: str
    followers: int | None
    active_followers: int | None
    engagement_total: int | None
    avg_engagement: float | None
    growth_rate: float | None
    score: float | None


class TopKolItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rank: int
    platform: str
    kol_uid: str
    nickname: str
    score: float | None
    engagement_total: int | None
    rating: str


class KolAnalysisData(UniqueKeyValidator):
    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: KolAnalysisSummary
    platform_distribution: tuple[DistributionItem, ...] = Field(default_factory=tuple)
    rating_distribution: tuple[DistributionItem, ...] = Field(default_factory=tuple)
    follower_distribution: tuple[DistributionItem, ...] = Field(default_factory=tuple)
    engagement_distribution: tuple[DistributionItem, ...] = Field(default_factory=tuple)
    region_distribution: tuple[DistributionItem, ...] = Field(default_factory=tuple)
    kol_trend: tuple[KolTrendItem, ...] = Field(default_factory=tuple, max_length=20)
    top_kols: tuple[TopKolItem, ...] = Field(default_factory=tuple, max_length=20)

    STABLE_KEYS = {
        "platform_distribution": ("key",),
        "rating_distribution": ("key",),
        "follower_distribution": ("key",),
        "engagement_distribution": ("key",),
        "region_distribution": ("key",),
    }


class KolAnalysisNarrative(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    executive_summary: str
    portfolio_findings: tuple[NarrativeFinding, ...] = Field(default_factory=tuple)
    mix_recommendations: tuple[NarrativeFinding, ...] = Field(default_factory=tuple)
    risk_notes: tuple[NarrativeFinding, ...] = Field(default_factory=tuple)


class KolAnalysisV2(ArtifactPayloadBase):
    schema_version: Literal["kol_analysis_v2"] = "kol_analysis_v2"
    module: Literal["kol"] = "kol"

    scope: KolAnalysisScope
    data: KolAnalysisData
    narrative: KolAnalysisNarrative

    REQUIRED_SECTIONS = frozenset({"summary", "kol_trend", "top_kols"})
    SECTION_NUMERIC_PATHS = {
        "summary": (
            "summary.kol_count",
            "summary.total_followers",
            "summary.total_active_followers",
            "summary.total_engagement",
            "summary.avg_score",
        ),
    }
