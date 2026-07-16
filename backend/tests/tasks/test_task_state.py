import pytest

from app.tasks.state import (
    InvalidTaskTransition,
    TERMINAL_TASK_STATUSES,
    TaskEventType,
    TaskStatus,
    ensure_transition,
)


def test_task_status_values_are_frozen() -> None:
    assert {item.value for item in TaskStatus} == {
        "pending",
        "planning",
        "running",
        "completed",
        "completed_with_warnings",
        "failed",
        "insufficient_balance",
        "interrupted",
        "cancelled",
    }


def test_task_event_type_values_are_frozen() -> None:
    assert {item.value for item in TaskEventType} == {
        "task.pending",
        "plan.ready",
        "tool.started",
        "tool.succeeded",
        "tool.failed",
        "tool.unknown",
        "points.reserved",
        "points.settled",
        "points.released",
        "candidates.updated",
        "bi.updated",
        "message.delta",
        "message.completed",
        "task.completed",
        "task.completed_with_warnings",
        "replan.ready",
        "task.failed",
        "task.cancelled",
    }


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (TaskStatus.PENDING, TaskStatus.PLANNING),
        (TaskStatus.PLANNING, TaskStatus.RUNNING),
        (TaskStatus.RUNNING, TaskStatus.COMPLETED),
        (TaskStatus.RUNNING, TaskStatus.INTERRUPTED),
        (TaskStatus.RUNNING, TaskStatus.CANCELLED),
    ],
)
def test_allowed_task_transitions(source: TaskStatus, target: TaskStatus) -> None:
    ensure_transition(source, target)


@pytest.mark.parametrize(
    "terminal",
    [
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.INSUFFICIENT_BALANCE,
        TaskStatus.CANCELLED,
    ],
)
def test_terminal_task_cannot_return_to_running(terminal: TaskStatus) -> None:
    with pytest.raises(InvalidTaskTransition):
        ensure_transition(terminal, TaskStatus.RUNNING)


def test_running_task_may_complete_with_warnings() -> None:
    ensure_transition(TaskStatus.RUNNING, TaskStatus.COMPLETED_WITH_WARNINGS)
    assert TaskStatus.COMPLETED_WITH_WARNINGS in TERMINAL_TASK_STATUSES


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("pending", TaskStatus.PLANNING),
        ("not-a-status", TaskStatus.PLANNING),
        (TaskStatus.PENDING, "planning"),
        (TaskStatus.PENDING, "not-a-status"),
    ],
)
def test_transition_rejects_non_task_status_values(source: object, target: object) -> None:
    with pytest.raises(InvalidTaskTransition):
        ensure_transition(source, target)  # type: ignore[arg-type]
