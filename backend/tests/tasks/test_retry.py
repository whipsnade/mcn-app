import pytest

from app.tasks.service import can_retry_status
from app.tasks.state import TERMINAL_TASK_STATUSES, TaskStatus


@pytest.mark.parametrize("status", tuple(TERMINAL_TASK_STATUSES))
def test_terminal_task_statuses_can_be_retried(status: TaskStatus) -> None:
    assert can_retry_status(status) is True


@pytest.mark.parametrize(
    "status",
    (TaskStatus.PENDING, TaskStatus.PLANNING, TaskStatus.RUNNING, TaskStatus.INTERRUPTED),
)
def test_non_terminal_task_statuses_are_not_retryable(status: TaskStatus) -> None:
    assert can_retry_status(status) is False
