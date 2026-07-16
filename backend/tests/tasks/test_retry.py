from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.tasks import router as tasks_router
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


@pytest.mark.asyncio
async def test_followup_retry_commits_snapshot_before_refreshing_pending_metadata(monkeypatch) -> None:
    task = SimpleNamespace(
        id="task-1",
        session_id="session-1",
        trigger_message_id="message-1",
        status="completed",
        estimated_points=0,
        error_code=None,
        error_message=None,
    )
    metadata = AsyncMock(side_effect=[
        {"followup_suggestions_status": "failed"},
        {"followup_suggestions_status": "pending", "followup_suggestions": []},
    ])
    monkeypatch.setattr(tasks_router.TaskRepository, "get_owned", AsyncMock(return_value=task))
    monkeypatch.setattr(tasks_router, "task_followup_metadata", metadata)
    db = AsyncMock()
    runner = SimpleNamespace(retry_followup=AsyncMock(return_value=True))

    result = await tasks_router.retry_followups(
        "task-1", SimpleNamespace(id="user-1"), db, runner,
    )

    db.commit.assert_awaited_once()
    db.refresh.assert_awaited_once_with(task)
    assert result.followup_suggestions_status == "pending"
