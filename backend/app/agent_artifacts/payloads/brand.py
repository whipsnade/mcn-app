"""brand_report_v3: 品牌八章节强类型 payload (spec §12.1)."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agent_artifacts.payloads.common import (
    ArtifactPayloadBase,
    ContentTypeItem,
    NarrativeFinding,
    NarrativeRecommendation,
    Period,
    SentimentSection,
    TopPost,
)


class BrandScope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    brand: str
    period: Period
    platforms: tuple[str, ...] = Field(default_factory=tuple)
    keywords: tuple[str, ...] = Field(default_factory=tuple)
    comparison_mode: Literal["none", "mom", "mom_yoy"]


class BrandPlatformMetric(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    platform: str
    volume: int | None
    engagement: int | None
    posts: int | None
    share_of_voice: float | None
    sentiment_score: float | None


class BrandOverview(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    total_volume: int | None
    total_engagement: int | None
    total_posts: int | None
    sentiment_score: float | None
    platforms: tuple[BrandPlatformMetric, ...] = Field(default_factory=tuple)


class ComparisonMetric(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    metric: str
    current: float | None
    baseline: float | None
    delta: float | None
    rate: float | None


class Comparison(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["not_requested", "complete", "partial", "unavailable"]
    baseline_period: Period | None = None
    metrics: tuple[ComparisonMetric, ...] = Field(default_factory=tuple)


class BrandComparisons(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mom: Comparison
    yoy: Comparison


class DailyTrendItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    date: date
    platform: str
    volume: int | None
    engagement: int | None
    positive: int | None
    neutral: int | None
    negative: int | None


class CreatorTierItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    platform: str
    tier: str
    creator_count: int | None
    posts: int | None
    volume: int | None
    engagement: int | None


class OrganicVsPaidItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    platform: str
    kind: str
    posts: int | None
    volume: int | None
    engagement: int | None


class RegionItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    region: str
    volume: int | None
    share: float | None
    sentiment_score: float | None


class TopicItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str
    volume: int | None
    engagement: int | None
    sentiment_score: float | None


class BrandData(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    overview: BrandOverview
    comparisons: BrandComparisons
    sentiment: SentimentSection
    daily_trend: tuple[DailyTrendItem, ...] = Field(default_factory=tuple)
    content_types: tuple[ContentTypeItem, ...] = Field(default_factory=tuple)
    creator_tiers: tuple[CreatorTierItem, ...] = Field(default_factory=tuple)
    organic_vs_paid: tuple[OrganicVsPaidItem, ...] = Field(default_factory=tuple)
    regions: tuple[RegionItem, ...] = Field(default_factory=tuple)
    topics: tuple[TopicItem, ...] = Field(default_factory=tuple)
    top_posts: tuple[TopPost, ...] = Field(default_factory=tuple, max_length=20)


class BrandNarrative(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    executive_summary: str
    findings: tuple[NarrativeFinding, ...] = Field(default_factory=tuple)
    recommendations: tuple[NarrativeRecommendation, ...] = Field(default_factory=tuple)


class BrandReportV3(ArtifactPayloadBase):
    schema_version: Literal["brand_report_v3"] = "brand_report_v3"
    module: Literal["brand"] = "brand"

    scope: BrandScope
    data: BrandData
    narrative: BrandNarrative

    REQUIRED_SECTIONS = frozenset({"overview", "sentiment", "daily_trend", "topics", "top_posts"})
    SECTION_NUMERIC_PATHS = {
        "overview": (
            "overview.total_volume",
            "overview.total_engagement",
            "overview.total_posts",
            "overview.sentiment_score",
        ),
        "sentiment": (
            "sentiment.summary.positive.count",
            "sentiment.summary.positive.share",
            "sentiment.summary.neutral.count",
            "sentiment.summary.neutral.share",
            "sentiment.summary.negative.count",
            "sentiment.summary.negative.share",
        ),
    }
