from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal


@dataclass(frozen=True)
class ThinkingOperationSpec:
    operation_id: str
    turn_id: str
    session_id: str
    user_id: str
    purpose: str
    label: str
    task_id: str | None = None
    goal_id: str | None = None


@dataclass(frozen=True)
class ThinkingBlock:
    operation_id: str
    turn_id: str
    purpose: str
    attempt: int
    label: str
    content: str
    status: Literal["completed", "interrupted"]
    started_at: datetime
    completed_at: datetime
    duration_ms: int
    task_id: str | None = None
    goal_id: str | None = None
    truncated: bool = False


ThinkingEventType = Literal[
    "thinking.started",
    "thinking.delta",
    "thinking.snapshot",
    "thinking.completed",
    "thinking.failed",
]


@dataclass(frozen=True)
class ThinkingEvent:
    id: str
    type: ThinkingEventType
    payload: dict[str, Any]


__all__ = [
    "ThinkingBlock",
    "ThinkingEvent",
    "ThinkingEventType",
    "ThinkingOperationSpec",
]
