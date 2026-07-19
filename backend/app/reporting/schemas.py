from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


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
