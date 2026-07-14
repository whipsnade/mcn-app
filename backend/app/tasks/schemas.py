from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.tasks.state import TaskEventType, TaskStatus


ScoringProfile = Literal[
    "balanced",
    "audience_first",
    "performance_first",
    "budget_first",
    "risk_first",
]


class TaskCreate(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)
    scoring_profile: ScoringProfile = "balanced"


class TaskRead(BaseModel):
    id: str
    session_id: str
    status: TaskStatus
    estimated_points: int
    error_code: str | None
    latest_report_id: str | None = None


class TaskEventRead(BaseModel):
    id: int
    task_id: str
    type: TaskEventType
    payload: dict[str, Any]
    created_at: datetime
