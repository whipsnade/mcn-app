from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator

from app.tasks.state import TaskEventType, TaskStatus
from app.workspace.schemas import MessageRead


class TaskCreate(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)
    turn_id: UUID = Field(default_factory=uuid4)

    @field_validator("content")
    @classmethod
    def reject_blank_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content_must_not_be_blank")
        return value


class TaskRead(BaseModel):
    id: str
    session_id: str
    trigger_message_id: str | None = None
    status: TaskStatus
    kind: Literal["pipeline", "agent"] = "pipeline"
    estimated_points: int
    error_code: str | None
    error_message: str | None = None
    latest_report_id: str | None = None
    followup_suggestions_status: Literal["pending", "completed", "failed"] | None = None
    followup_suggestions: list[dict[str, str]] = Field(default_factory=list)
    followup_error: dict[str, Any] | None = None


class TaskEventRead(BaseModel):
    id: int
    task_id: str
    type: TaskEventType
    payload: dict[str, Any]
    created_at: datetime


class TaskOutcomeTask(BaseModel):
    """create_task 已建任务（enforce 关闭或 planner execute）。"""

    outcome: Literal["task"] = "task"
    task: TaskRead


class TaskOutcomeClarify(BaseModel):
    """create_task planner 澄清：不落任务，返回落库的 assistant 澄清消息。"""

    outcome: Literal["clarify"] = "clarify"
    message: MessageRead


class TaskOutcomeRespond(BaseModel):
    """create_task planner 对话式回复：不落任务，返回落库的 assistant 回复消息。"""

    outcome: Literal["respond"] = "respond"
    respond_type: Literal["context_qa", "usage_help", "out_of_scope"]
    message: MessageRead


TaskCreateResult = TaskOutcomeTask | TaskOutcomeClarify | TaskOutcomeRespond
