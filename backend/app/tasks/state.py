from enum import StrEnum


class TaskStatus(StrEnum):
    PENDING = "pending"
    PLANNING = "planning"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    FAILED = "failed"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


class TaskEventType(StrEnum):
    TASK_PENDING = "task.pending"
    PLAN_READY = "plan.ready"
    TOOL_STARTED = "tool.started"
    TOOL_SUCCEEDED = "tool.succeeded"
    TOOL_FAILED = "tool.failed"
    TOOL_UNKNOWN = "tool.unknown"
    POINTS_RESERVED = "points.reserved"
    POINTS_SETTLED = "points.settled"
    POINTS_RELEASED = "points.released"
    CANDIDATES_UPDATED = "candidates.updated"
    BI_UPDATED = "bi.updated"
    MESSAGE_DELTA = "message.delta"
    MESSAGE_COMPLETED = "message.completed"
    TASK_COMPLETED = "task.completed"
    TASK_COMPLETED_WITH_WARNINGS = "task.completed_with_warnings"
    REPLAN_READY = "replan.ready"
    TASK_FAILED = "task.failed"
    TASK_CANCELLED = "task.cancelled"


TERMINAL_TASK_STATUSES = {
    TaskStatus.COMPLETED,
    TaskStatus.COMPLETED_WITH_WARNINGS,
    TaskStatus.FAILED,
    TaskStatus.INSUFFICIENT_BALANCE,
    TaskStatus.CANCELLED,
}

ALLOWED_TRANSITIONS = {
    TaskStatus.PENDING: {TaskStatus.PLANNING, TaskStatus.CANCELLED},
    TaskStatus.PLANNING: {
        TaskStatus.RUNNING,
        TaskStatus.FAILED,
        TaskStatus.INSUFFICIENT_BALANCE,
        TaskStatus.INTERRUPTED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.RUNNING: {
        TaskStatus.COMPLETED,
        TaskStatus.COMPLETED_WITH_WARNINGS,
        TaskStatus.FAILED,
        TaskStatus.INSUFFICIENT_BALANCE,
        TaskStatus.INTERRUPTED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.INTERRUPTED: {
        TaskStatus.PLANNING,
        TaskStatus.RUNNING,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
}


class InvalidTaskTransition(ValueError):
    pass


def ensure_transition(source: TaskStatus, target: TaskStatus) -> None:
    if not isinstance(source, TaskStatus) or not isinstance(target, TaskStatus):
        raise InvalidTaskTransition(f"{source!r}->{target!r}")
    if target not in ALLOWED_TRANSITIONS.get(source, set()):
        raise InvalidTaskTransition(f"{source.value}->{target.value}")
