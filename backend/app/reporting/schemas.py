from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


EvidencePriority = tuple[float, str, str]


@dataclass(frozen=True)
class ToolEvidence:
    """一条已经由 MCP 网关校验成功的结构化证据。"""

    internal_tool_name: str
    payload: dict[str, Any]
    source_call_id: str | None
    collected_at: datetime


@dataclass(frozen=True)
class NormalizedBrandEvidence:
    """仅保存品牌 BI 白名单字段，不保存 DataTap 原始载荷。"""

    tool_name: str
    platform: str
    period: str | None
    analytics_fields: dict[str, Any]
    evidence_references: tuple[str, ...]
    collected_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "platform": self.platform,
            "period": self.period,
            "analytics_fields": self.analytics_fields,
            "evidence_references": list(self.evidence_references),
            "collected_at": self.collected_at.isoformat(),
        }


@dataclass(frozen=True)
class DimensionInputs:
    audience: float | None
    content: float | None
    engagement: float | None
    budget: float | None
    growth: float | None
    brand_safety: float | None

    def as_mapping(self) -> dict[str, float | None]:
        return {
            "audience": self.audience,
            "content": self.content,
            "engagement": self.engagement,
            "budget": self.budget,
            "growth": self.growth,
            "brand_safety": self.brand_safety,
        }


@dataclass(frozen=True)
class DimensionScore:
    raw_score: float | None
    weight: int
    weighted_score: float


@dataclass(frozen=True)
class CandidateScore:
    version: str
    total: float
    weights: dict[str, int]
    dimensions: dict[str, DimensionScore]
    data_completeness: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "total": self.total,
            "weights": self.weights,
            "dimensions": {
                name: {
                    "raw_score": value.raw_score,
                    "weight": value.weight,
                    "weighted_score": value.weighted_score,
                }
                for name, value in self.dimensions.items()
            },
            "data_completeness": self.data_completeness,
        }


@dataclass(frozen=True)
class NormalizedKolEvidence:
    platform: str
    platform_account_id: str
    nickname: str | None
    normalized_profile_url: str | None
    followers: int | None
    engagement_rate: float | None
    quoted_price_cny: float | None
    content_score: float | None
    audience_score: float | None
    engagement_score: float | None
    budget_score: float | None
    growth_score: float | None
    brand_safety_score: float | None
    risk_flags: tuple[dict[str, Any], ...]
    collected_at: datetime
    evidence_references: tuple[str, ...]
    missing_fields: tuple[str, ...]
    # 仅保存导出模板需要的、经过脱敏的字段；不保留 MCP 原始响应。
    export_fields: dict[str, Any] = field(default_factory=dict)
    # 仅保存 BI 契约白名单内、经过形状校验和脱敏的规范字段。
    analytics_fields: dict[str, Any] = field(default_factory=dict)
    # 仅在本轮内存合并使用，不进入持久化字典，也不参与领域对象相等比较。
    evidence_priority: EvidencePriority | None = field(
        default=None,
        compare=False,
        repr=False,
    )
    field_provenance: tuple[tuple[str, EvidencePriority], ...] = field(
        default_factory=tuple,
        compare=False,
        repr=False,
    )

    def dimensions(self) -> DimensionInputs:
        return DimensionInputs(
            audience=self.audience_score,
            content=self.content_score,
            engagement=self.engagement_score,
            budget=self.budget_score,
            growth=self.growth_score,
            brand_safety=self.brand_safety_score,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "platform_account_id": self.platform_account_id,
            "nickname": self.nickname,
            "normalized_profile_url": self.normalized_profile_url,
            "followers": self.followers,
            "engagement_rate": self.engagement_rate,
            "quoted_price_cny": self.quoted_price_cny,
            "content_score": self.content_score,
            "audience_score": self.audience_score,
            "engagement_score": self.engagement_score,
            "budget_score": self.budget_score,
            "growth_score": self.growth_score,
            "brand_safety_score": self.brand_safety_score,
            "risk_flags": list(self.risk_flags),
            "collected_at": self.collected_at.isoformat(),
            "evidence_references": list(self.evidence_references),
            "missing_fields": list(self.missing_fields),
            "export_fields": self.export_fields,
            "analytics_fields": self.analytics_fields,
        }


@dataclass(frozen=True)
class CandidateVersionItem:
    platform: str
    platform_account_id: str
    rank: int
    total_score: float
    snapshot_id: str
    kol_id: str


@dataclass(frozen=True)
class CandidateVersion:
    candidate_version: int
    evidence_digest: str
    candidates: tuple[CandidateVersionItem, ...]


class CandidateRead(BaseModel):
    id: str
    kol_id: str
    platform: str
    platform_account_id: str
    nickname: str | None = None
    profile_url: str | None = None
    rank: int
    total_score: float
    scores: dict[str, float | None]
    matched_conditions: list[str]
    risks: list[dict[str, Any]]
    recommendation: str
    metrics: dict[str, int | float | str | None]


class CandidatePage(BaseModel):
    task_id: str
    version: int
    total: int
    items: list[CandidateRead]


class CandidateVersionSummary(BaseModel):
    task_id: str
    version: int
    total: int


class TaskAnalysisSummary(BaseModel):
    id: str
    status: str
    kind: Literal["pipeline", "agent"] = "pipeline"
    completed_at: datetime | None = None
    followup_suggestions_status: str | None = None
    followup_suggestions: list[dict[str, Any]] = Field(default_factory=list)
    followup_error: dict[str, Any] | None = None


class AnalysisReportRead(BaseModel):
    id: str
    task_id: str
    version: int
    title: str
    blocks: list[dict[str, Any]]
    conclusion: str | None = None
    status: str
    generated_at: datetime


class AnalysisReportSummary(BaseModel):
    id: str
    task_id: str
    version: int
    title: str
    status: str
    generated_at: datetime


class BiReportRead(BaseModel):
    id: str
    task_id: str
    report_version: int
    candidate_version: int
    overview: dict[str, Any]
    score_composition: list[dict[str, Any]]
    audience_content_fit: dict[str, Any]
    platform_distribution: list[dict[str, Any]]
    budget_analysis: dict[str, Any]
    comparison: list[dict[str, Any]]
    risks: list[dict[str, Any]]
    analytics: dict[str, Any]
    analysis_scope: Literal["brand", "kol", "hybrid"] = "kol"
    brand_analytics: dict[str, Any] = Field(default_factory=dict)
    kol_analytics: dict[str, Any] = Field(default_factory=dict)
    data_availability: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    conclusion: str
    sources: list[dict[str, Any]]
    generated_at: datetime


class BiReportSummary(BaseModel):
    id: str
    task_id: str
    report_version: int
    candidate_version: int
    status: str
    generated_at: datetime


class FavoriteCreate(BaseModel):
    kol_id: str = Field(min_length=1, max_length=36)
    note: str | None = Field(default=None, max_length=500)
    source_task_id: str | None = Field(default=None, min_length=1, max_length=36)


class FavoriteRead(BaseModel):
    kol_id: str
    nickname: str | None = None
    platform: str
    platform_account_id: str
    profile_url: str | None = None
    note: str | None = None
    source_task_id: str | None = None
    created_at: datetime


class AnalystConclusion(BaseModel):
    """模型仅可补充可追溯的结论，不能修改候选集合、评分或版本。"""

    conclusion: str = Field(min_length=1, max_length=2_000)
    caveats: list[str] = Field(default_factory=list, max_length=10)
