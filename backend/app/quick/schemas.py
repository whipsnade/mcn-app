from typing import Any

from pydantic import BaseModel, Field


class KolRecommendationItem(BaseModel):
    platform: str
    kw_uid: str
    nickname: str | None
    fans: int | None
    price: float | None
    engagement_rate: float | None
    score: float | None
    city: str | None
    tags: list[str] = Field(default_factory=list)


class KolRecommendationsResponse(BaseModel):
    items: list[KolRecommendationItem]
    points_cost: int


class TopPostItem(BaseModel):
    title: str | None
    nickname: str | None
    interact: float | None
    like: float | None
    comment: float | None
    collect: float | None
    publish_time: str | None
    url: str | None
    platform: str


class KolDetailResponse(BaseModel):
    detail: dict[str, Any]
    posts: list[TopPostItem]
    points_cost: int
    posts_degraded: bool = False


class TopPostsResponse(BaseModel):
    items: list[TopPostItem]
    points_cost: int
    # insight-cube 原帖查询失败时的跨网关降级：返回同行业热门达人代替。
    degraded: bool = False
    fallback_kols: list[KolRecommendationItem] = Field(default_factory=list)


class EvaluateResponse(BaseModel):
    title: str
    analysis_markdown: str
