"""campaign_report_v2: 活动分析强类型 payload (spec §12.1)."""

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


class CampaignScope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    brand: str
    campaign: str
    period: Period
    platforms: tuple[str, ...] = Field(default_factory=tuple)
    keywords: tuple[str, ...] = Field(default_factory=tuple)


class CampaignOverview(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    total_volume: int | None
    total_engagement: int | None
    total_posts: int | None
    total_creators: int | None
    sentiment_score: float | None


class PlatformContribution(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    platform: str
    volume: int | None
    engagement: int | None
    posts: int | None
    creators: int | None
    share: float | None


class CampaignTimelineItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    date: date
    platform: str
    volume: int | None
    engagement: int | None
    posts: int | None


class KolContribution(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    platform: str
    kol_uid: str
    nickname: str
    posts: int | None
    volume: int | None
    engagement: int | None
    contribution_share: float | None


class CampaignData(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    overview: CampaignOverview
    platform_contributions: tuple[PlatformContribution, ...] = Field(default_factory=tuple)
    timeline: tuple[CampaignTimelineItem, ...] = Field(default_factory=tuple)
    kol_contributions: tuple[KolContribution, ...] = Field(default_factory=tuple, max_length=20)
    content_types: tuple[ContentTypeItem, ...] = Field(default_factory=tuple)
    sentiment: SentimentSection
    top_posts: tuple[TopPost, ...] = Field(default_factory=tuple, max_length=20)


class PhaseReview(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    phase: str
    detail: str
    supporting_paths: tuple[str, ...] = Field(default_factory=tuple)


class CampaignNarrative(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    executive_summary: str
    phase_review: tuple[PhaseReview, ...] = Field(default_factory=tuple)
    findings: tuple[NarrativeFinding, ...] = Field(default_factory=tuple)
    recommendations: tuple[NarrativeRecommendation, ...] = Field(default_factory=tuple)


class CampaignReportV2(ArtifactPayloadBase):
    schema_version: Literal["campaign_report_v2"] = "campaign_report_v2"
    module: Literal["campaign"] = "campaign"

    scope: CampaignScope
    data: CampaignData
    narrative: CampaignNarrative

    REQUIRED_SECTIONS = frozenset(
        {"overview", "platform_contributions", "timeline", "sentiment", "top_posts"}
    )
    SECTION_NUMERIC_PATHS = {
        "overview": (
            "overview.total_volume",
            "overview.total_engagement",
            "overview.total_posts",
            "overview.total_creators",
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
