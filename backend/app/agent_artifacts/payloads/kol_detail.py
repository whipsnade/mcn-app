"""kol_detail_v2: 达人详情强类型 payload (spec §12.1)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agent_artifacts.payloads.common import (
    ArtifactPayloadBase,
    NarrativeFinding,
    OptionalHttpUrl,
    UniqueKeyValidator,
)


class KolDetailScope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    platform: str
    kol_uid: str
    selection_artifact_id: str | None = None
    selection_version: str | None = None


class KolIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    nickname: str
    avatar_url: OptionalHttpUrl = None
    homepage_url: OptionalHttpUrl = None
    bio: str
    verification: bool
    region: str


class KolMetrics(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    followers: int | None
    following: int | None
    posts: int | None
    likes: int | None
    active_followers: int | None
    active_follower_rate: float | None
    growth_rate: float | None
    engagement_total: int | None
    avg_engagement: float | None


class KolAudienceDistribution(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    label: str
    value: int | None
    share: float | None


class KolAudience(UniqueKeyValidator):
    model_config = ConfigDict(frozen=True, extra="forbid")

    gender_distribution: tuple[KolAudienceDistribution, ...] = Field(default_factory=tuple)
    age_distribution: tuple[KolAudienceDistribution, ...] = Field(default_factory=tuple)
    region_distribution: tuple[KolAudienceDistribution, ...] = Field(default_factory=tuple)
    interest_distribution: tuple[KolAudienceDistribution, ...] = Field(default_factory=tuple)

    STABLE_KEYS = {
        "gender_distribution": ("key",),
        "age_distribution": ("key",),
        "region_distribution": ("key",),
        "interest_distribution": ("key",),
    }


class KolTrendItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    date: date
    followers: int | None = None
    engagement: int | None = None
    posts: int | None = None


class LatestPost(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    post_id: str
    title: str
    url: OptionalHttpUrl
    published_at: datetime
    likes: int | None
    comments: int | None
    shares: int | None
    engagement: int | None


class KolDetailCache(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    hit: bool
    fetched_at: datetime
    expires_at: datetime


class KolDetailData(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: KolIdentity
    metrics: KolMetrics
    audience: KolAudience
    trend: tuple[KolTrendItem, ...] = Field(default_factory=tuple)
    latest_posts: tuple[LatestPost, ...] = Field(default_factory=tuple, max_length=5)
    cache: KolDetailCache


class KolDetailNarrative(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_summary: str
    content_strengths: tuple[NarrativeFinding, ...] = Field(default_factory=tuple)
    commercial_notes: tuple[NarrativeFinding, ...] = Field(default_factory=tuple)
    risk_notes: tuple[NarrativeFinding, ...] = Field(default_factory=tuple)


class KolDetailV2(ArtifactPayloadBase):
    schema_version: Literal["kol_detail_v2"] = "kol_detail_v2"
    module: Literal["kol"] = "kol"

    scope: KolDetailScope
    data: KolDetailData
    narrative: KolDetailNarrative

    REQUIRED_SECTIONS = frozenset({"identity", "metrics", "audience", "trend", "latest_posts"})
    # §6.3：cache 是运行时元数据（hit/fetched_at/expires_at），不要求治理。
    GOVERNED_SECTIONS = frozenset({"identity", "metrics", "audience", "trend", "latest_posts"})
