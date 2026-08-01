from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.goals.models import TaskGoal
from app.tasks import router as tasks_router
from app.tasks.models import AnalysisTask
from app.tasks.service import (
    can_retry_status,
    idempotency_key_digest,
    idempotency_payload_digest,
    TaskService,
    TaskConflictError,
)
from app.tasks.state import TERMINAL_TASK_STATUSES, TaskStatus
from app.workspace.models import Message, WorkspaceSession


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

        async def find_idempotent(self, user_id, session_id, idempotency_key):
            # 幂等命中：跳过 planner，直接进入 create_idempotent 复用路径。
            assert (user_id, session_id, idempotency_key) == ("user-1", "session-1", "browser-key")
            return task

        async def create_idempotent(
            self, user_id, session_id, payload, idempotency_key, **_kwargs
        ):
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

    assert result.outcome == "task"
    assert result.task.id == "task-existing"
    runner.submit.assert_not_called()


@pytest.mark.asyncio
async def test_create_task_returns_409_for_same_key_with_different_payload(monkeypatch) -> None:
    class StubTaskService:
        def __init__(self, db):
            self.db = db

        async def find_idempotent(self, user_id, session_id, idempotency_key):
            # 幂等命中：跳过 planner；create_idempotent 负责 payload 一致性校验。
            return SimpleNamespace(id="task-existing")

        async def create_idempotent(
            self, user_id, session_id, payload, idempotency_key, **_kwargs
        ):
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


# ---------------------------------------------------------------------------
# retry 复制 goal 结构（阶段四）
# ---------------------------------------------------------------------------


async def _seed_source_task(db_session, user_factory, goal_rows: list[dict]):
    """造一个终态源任务 + 指定 goal 行，返回 (user_id, source_task, source_goals)。"""
    from datetime import UTC, datetime

    user = await user_factory()
    now = datetime.now(UTC).replace(tzinfo=None)
    session = WorkspaceSession(
        id=str(uuid4()),
        user_id=user.id,
        title="retry goal 结构测试",
        brand="海底捞",
        campaign_name=None,
        status="active",
        platforms=["xiaohongshu"],
        category="美食",
        target_audience="",
        budget_min=None,
        budget_max=None,
        filters_snapshot={},
        is_starred=False,
        last_accessed_at=now,
        created_at=now,
        updated_at=now,
    )
    db_session.add(session)
    await db_session.flush()
    message = Message(
        id=str(uuid4()),
        session_id=session.id,
        user_id=user.id,
        role="user",
        content="复盘海底捞 618 并圈选达人",
        sequence=1,
        metadata_json={},
        created_at=now,
    )
    db_session.add(message)
    await db_session.flush()
    task = AnalysisTask(
        id=str(uuid4()),
        user_id=user.id,
        session_id=session.id,
        trigger_message_id=message.id,
        status="completed",
        kind="agent",
        max_calls=10,
        estimated_points=0,
        creation_order=1,
        created_at=now,
        updated_at=now,
    )
    db_session.add(task)
    await db_session.flush()
    goals: list[TaskGoal] = []
    id_by_sequence: dict[int, str] = {}
    for row in goal_rows:
        goal = TaskGoal(
            id=str(uuid4()),
            task_id=task.id,
            sequence=row["sequence"],
            goal_type=row["goal_type"],
            status="completed",
            depends_on_goal_id=(
                id_by_sequence.get(row["depends_on_sequence"])
                if row.get("depends_on_sequence")
                else None
            ),
            params_json=row["params"],
            result_summary_json=None,
            started_at=now,
            completed_at=now,
            created_at=now,
            updated_at=now,
        )
        db_session.add(goal)
        await db_session.flush()
        id_by_sequence[goal.sequence] = goal.id
        goals.append(goal)
    return user.id, task, goals


@pytest.mark.asyncio
async def test_retry_multi_goal_task_copies_goal_structure(db_session, user_factory) -> None:
    user_id, source_task, source_goals = await _seed_source_task(
        db_session,
        user_factory,
        [
            {
                "sequence": 1,
                "goal_type": "campaign_analysis",
                "params": {"brand": "海底捞", "category": "美食", "campaign": "618大促"},
            },
            {
                "sequence": 2,
                "goal_type": "kol_selection",
                "depends_on_sequence": 1,
                "params": {"brand": "海底捞", "category": "美食"},
            },
        ],
    )

    retry = await TaskService(db_session).retry(user_id, source_task.id)

    assert retry.id != source_task.id
    new_goals = list(
        (
            await db_session.scalars(
                select(TaskGoal)
                .where(TaskGoal.task_id == retry.id)
                .order_by(TaskGoal.sequence)
            )
        ).all()
    )
    assert [goal.goal_type for goal in new_goals] == ["campaign_analysis", "kol_selection"]
    assert [goal.sequence for goal in new_goals] == [1, 2]
    assert all(goal.status == "pending" for goal in new_goals)
    assert {goal.id for goal in new_goals}.isdisjoint({goal.id for goal in source_goals})
    assert new_goals[0].params_json == source_goals[0].params_json
    assert new_goals[1].params_json == source_goals[1].params_json
    # 依赖在新批内重新解析（不指向源任务的 goal）。
    assert new_goals[0].depends_on_goal_id is None
    assert new_goals[1].depends_on_goal_id == new_goals[0].id
    assert new_goals[1].depends_on_goal_id != source_goals[0].id


@pytest.mark.asyncio
async def test_retry_brand_task_copies_goal_type(db_session, user_factory) -> None:
    user_id, source_task, _ = await _seed_source_task(
        db_session,
        user_factory,
        [
            {
                "sequence": 1,
                "goal_type": "brand_analysis",
                "params": {"brand": "海底捞", "category": "美食"},
            },
        ],
    )

    retry = await TaskService(db_session).retry(user_id, source_task.id)

    goal = await db_session.scalar(select(TaskGoal).where(TaskGoal.task_id == retry.id))
    assert goal is not None
    assert goal.goal_type == "brand_analysis"
    assert goal.status == "pending"
    assert goal.params_json == {"brand": "海底捞", "category": "美食"}


@pytest.mark.asyncio
async def test_retry_legacy_task_without_goals_keeps_default(db_session, user_factory) -> None:
    user_id, source_task, _ = await _seed_source_task(db_session, user_factory, [])

    retry = await TaskService(db_session).retry(user_id, source_task.id)

    goal = await db_session.scalar(select(TaskGoal).where(TaskGoal.task_id == retry.id))
    assert goal is not None
    assert goal.goal_type == "kol_selection"
    assert goal.sequence == 1
    assert goal.params_json == {"brand": "海底捞", "category": "美食"}
