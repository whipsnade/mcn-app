from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.tasks import router as tasks_router
from app.tasks.service import (
    can_retry_status,
    idempotency_key_digest,
    idempotency_payload_digest,
    TaskService,
    TaskConflictError,
)
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


def test_idempotency_digest_is_stable_and_does_not_expose_raw_key_or_payload() -> None:
    key = "  browser-retry-42  "
    assert idempotency_key_digest(key) == idempotency_key_digest(key.strip())
    assert len(idempotency_key_digest(key)) == 64
    digest = idempotency_payload_digest("  找达人  ")
    assert digest == idempotency_payload_digest("找达人")
    assert len(digest) == 64
    assert key.strip() not in digest


@pytest.mark.asyncio
async def test_create_idempotent_reuses_same_payload_and_rejects_mismatch(monkeypatch) -> None:
    existing = SimpleNamespace(
        id="task-existing",
        idempotency_payload_hash=idempotency_payload_digest("找达人"),
    )
    monkeypatch.setattr(
        "app.tasks.service.WorkspaceService.get_owned_session",
        AsyncMock(return_value=SimpleNamespace(id="session-1")),
    )
    db = AsyncMock()
    db.scalar = AsyncMock(return_value=existing)
    service = TaskService(db)

    task, reused = await service.create_idempotent(
        "user-1", "session-1", tasks_router.TaskCreate(content="  找达人 "), "same-key",
    )
    assert (task.id, reused) == ("task-existing", True)

    with pytest.raises(TaskConflictError, match="idempotency_payload_mismatch"):
        await service.create_idempotent(
            "user-1", "session-1", tasks_router.TaskCreate(content="换一个问题"), "same-key",
        )


@pytest.mark.asyncio
async def test_create_task_reuses_idempotent_task_without_resubmitting(monkeypatch) -> None:
    task = SimpleNamespace(
        id="task-existing",
        session_id="session-1",
        trigger_message_id="message-existing",
        status="pending",
        estimated_points=0,
        error_code=None,
        error_message=None,
    )

    class StubTaskService:
        def __init__(self, db):
            self.db = db

        async def create_idempotent(self, user_id, session_id, payload, idempotency_key):
            assert (user_id, session_id, idempotency_key) == ("user-1", "session-1", "browser-key")
            return task, True

    monkeypatch.setattr(tasks_router, "TaskService", StubTaskService)
    db = AsyncMock()
    runner = SimpleNamespace(submit=AsyncMock())

    result = await tasks_router.create_task(
        "session-1",
        tasks_router.TaskCreate(content="找达人"),
        SimpleNamespace(id="user-1"),
        db,
        runner,
        "browser-key",
    )

    assert result.id == "task-existing"
    runner.submit.assert_not_called()


@pytest.mark.asyncio
async def test_create_task_returns_409_for_same_key_with_different_payload(monkeypatch) -> None:
    class StubTaskService:
        def __init__(self, db):
            self.db = db

        async def create_idempotent(self, user_id, session_id, payload, idempotency_key):
            raise TaskConflictError("idempotency_payload_mismatch")

    monkeypatch.setattr(tasks_router, "TaskService", StubTaskService)
    db = AsyncMock()
    runner = SimpleNamespace(submit=AsyncMock())

    with pytest.raises(tasks_router.HTTPException) as error:
        await tasks_router.create_task(
            "session-1",
            tasks_router.TaskCreate(content="另一条问题"),
            SimpleNamespace(id="user-1"),
            db,
            runner,
            "same-key",
        )

    assert error.value.status_code == 409
    assert error.value.detail == "幂等键对应的请求参数不一致"
    runner.submit.assert_not_called()
