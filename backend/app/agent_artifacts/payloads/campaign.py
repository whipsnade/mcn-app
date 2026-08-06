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
    # Gate C Task 4：排除规则/官方账号/对比模式/归属规则/用户补充资料版本。
    exclusions: tuple[str, ...] = Field(default_factory=tuple)
    official_accounts: tuple[str, ...] = Field(default_factory=tuple)
    comparison_mode: str | None = None
    attribution_rules: tuple[str, ...] = Field(default_factory=tuple)
    upload_ids: tuple[str, ...] = Field(default_factory=tuple)


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


class ComparisonSeries(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    metric: str
    current: float | None
    baseline: float | None
    delta: float | None
    rate: float | None


class PeriodComparisons(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # 活动期 vs 活动前等长周期 / 活动后观察期。
    current_baseline: tuple[ComparisonSeries, ...] = Field(default_factory=tuple)
    current_post: tuple[ComparisonSeries, ...] = Field(default_factory=tuple)


class Attribution(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    paid_confirmed: int | None = None
    organic: int | None = None
    unknown: int | None = None
    paid_confirmed_share: float | None = None


class OrganicSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    volume: int | None = None
    engagement: int | None = None
    posts: int | None = None
    share_of_volume: float | None = None


class AudienceRegion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    region: str
    volume: int | None = None
    share: float | None = None


class InternalMetrics(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    spend: float | None = None
    impressions: int | None = None
    conversions: int | None = None
    revenue: float | None = None
    cpc: float | None = None
    cpm: float | None = None


class RoiSection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    spend: float
    revenue: float | None = None
    conversions: int | None = None
    attribution_window: str
    # ROI 只有 spend + conversion/revenue + 归因窗口齐全时才生成；否则为 None。
    roi: float | None = None
    roas: float | None = None


class CampaignData(UniqueKeyValidator):
    model_config = ConfigDict(frozen=True, extra="forbid")

    overview: CampaignOverview
    platform_contributions: tuple[PlatformContribution, ...] = Field(default_factory=tuple)
    timeline: tuple[CampaignTimelineItem, ...] = Field(default_factory=tuple)
    kol_contributions: tuple[KolContribution, ...] = Field(default_factory=tuple, max_length=20)
    content_types: tuple[ContentTypeItem, ...] = Field(default_factory=tuple)
    sentiment: SentimentSection
    top_posts: tuple[TopPost, ...] = Field(default_factory=tuple, max_length=20)
    # Gate C Task 4：对比/归属/自然传播/受众/内部指标/ROI（均向后兼容默认值）。
    comparisons: PeriodComparisons = Field(default_factory=PeriodComparisons)
    attribution: Attribution | None = None
    organic_summary: OrganicSummary | None = None
    audience_regions: tuple[AudienceRegion, ...] = Field(default_factory=tuple)
    internal_metrics: InternalMetrics | None = None
    roi: RoiSection | None = None

    STABLE_KEYS = {
        "top_posts": ("platform", "post_id"),
        "kol_contributions": ("platform", "kol_uid"),
        "audience_regions": ("region",),
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
            "comparisons",
            "attribution",
            "organic_summary",
            "audience_regions",
            "internal_metrics",
            "roi",
        }
    )
