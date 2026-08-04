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
    UniqueKeyValidator,
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


class CampaignData(UniqueKeyValidator):
    model_config = ConfigDict(frozen=True, extra="forbid")

    overview: CampaignOverview
    platform_contributions: tuple[PlatformContribution, ...] = Field(default_factory=tuple)
    timeline: tuple[CampaignTimelineItem, ...] = Field(default_factory=tuple)
    kol_contributions: tuple[KolContribution, ...] = Field(default_factory=tuple, max_length=20)
    content_types: tuple[ContentTypeItem, ...] = Field(default_factory=tuple)
    sentiment: SentimentSection
    top_posts: tuple[TopPost, ...] = Field(default_factory=tuple, max_length=20)

    STABLE_KEYS = {
        "top_posts": ("platform", "post_id"),
        "kol_contributions": ("platform", "kol_uid"),
    }


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
    # §6.3：全部业务章节根纳入递归 null 治理（含数组元素内的 Optional 数值叶子）。
    GOVERNED_SECTIONS = frozenset(
        {
            "overview",
            "platform_contributions",
            "timeline",
            "kol_contributions",
            "content_types",
            "sentiment",
            "top_posts",
        }
    )
