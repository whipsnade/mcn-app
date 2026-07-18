from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.reporting.schemas import (
    AnalysisReportSummary,
    BiReportSummary,
    CandidateVersionSummary,
    TaskAnalysisSummary,
)


Platform = Literal["xiaohongshu", "douyin", "bilibili", "weibo", "wechat"]


class SessionCreate(BaseModel):
    brand: str = Field(default="", max_length=100)
    campaign_name: str | None = Field(default=None, min_length=1, max_length=120)
    platforms: list[Platform] = Field(default_factory=list)
    # Optional since agent tasks: free-form questions may not name a category.
    category: str | None = Field(default=None, max_length=100)
    target_audience: str = Field(default="", max_length=500)
    budget_min: Decimal | None = Field(default=None, ge=0)
    budget_max: Decimal | None = Field(default=None, ge=0)
    initial_query: str = Field(min_length=1, max_length=5000)
    filters: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_budget_range(self) -> "SessionCreate":
        if (
            self.budget_min is not None
            and self.budget_max is not None
            and self.budget_min > self.budget_max
        ):
            raise ValueError("budget_min_must_not_exceed_budget_max")
        return self


class SessionUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    brand: str | None = Field(default=None, min_length=1, max_length=100)
    campaign_name: str | None = Field(default=None, min_length=1, max_length=120)
    platforms: list[Platform] | None = Field(default=None, min_length=1)
    category: str | None = Field(default=None, min_length=1, max_length=100)
    target_audience: str | None = Field(default=None, min_length=1, max_length=500)
    budget_min: Decimal | None = Field(default=None, ge=0)
    budget_max: Decimal | None = Field(default=None, ge=0)
    is_starred: bool | None = None
    status: Literal["draft", "analyzing", "completed", "archived"] | None = None


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=20000)


class MessageRead(BaseModel):
    id: str
    role: Literal["user", "assistant", "system"]
    content: str
    sequence: int
    metadata: dict[str, Any]
    created_at: datetime


class SessionRead(BaseModel):
    id: str
    title: str
    brand: str
    campaign_name: str | None
    status: str
    platforms: list[str]
    category: str | None
    target_audience: str
    budget_min: Decimal | None
    budget_max: Decimal | None
    filters: dict[str, Any]
    is_starred: bool
    messages: list[MessageRead]
    latest_task: TaskAnalysisSummary | None = None
    latest_candidates: CandidateVersionSummary | None = None
    latest_report: BiReportSummary | None = None
    latest_analysis_report: AnalysisReportSummary | None = None
    created_at: datetime
    updated_at: datetime
