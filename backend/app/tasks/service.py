from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.tasks.models import AnalysisTask
from app.tasks.repository import TaskRepository
from app.tasks.schemas import TaskCreate
from app.tasks.state import TaskEventType, TaskStatus
from app.workspace.schemas import MessageCreate
from app.workspace.service import WorkspaceService


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class TaskConflictError(RuntimeError):
    pass


class TaskService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repository = TaskRepository(db)

    async def create(
        self, user_id: str, session_id: str, payload: TaskCreate
    ) -> AnalysisTask:
        workspace_service = WorkspaceService(self.db)
        await workspace_service.get_owned_session(user_id, session_id, for_update=True)
        active_task_id = await self.db.scalar(
            select(AnalysisTask.id)
            .where(
                AnalysisTask.session_id == session_id,
                AnalysisTask.status.notin_(
                    [
                        TaskStatus.COMPLETED,
                        TaskStatus.FAILED,
                        TaskStatus.INSUFFICIENT_BALANCE,
                        TaskStatus.CANCELLED,
                    ]
                ),
            )
            .limit(1)
        )
        if active_task_id is not None:
            raise TaskConflictError("task_in_progress")
        message = await workspace_service.append_message(
            user_id, session_id, MessageCreate(content=payload.content)
        )
        message.metadata_json = {"scoring_profile": payload.scoring_profile}
        now = utc_now()
        task = AnalysisTask(
            id=str(uuid4()),
            user_id=user_id,
            session_id=session_id,
            trigger_message_id=message.id,
            status=TaskStatus.PENDING,
            plan_json=None,
            plan_version=None,
            max_calls=10,
            estimated_points=0,
            error_code=None,
            error_message=None,
            cancel_requested_at=None,
            lease_owner=None,
            lease_expires_at=None,
            started_at=None,
            completed_at=None,
            created_at=now,
            updated_at=now,
        )
        self.db.add(task)
        await self.db.flush()
        await self.repository.append_event(
            task.id,
            task.user_id,
            TaskEventType.TASK_PENDING,
            {"status": TaskStatus.PENDING},
        )
        return task

    async def cancel(self, user_id: str, task_id: str) -> AnalysisTask:
        task = await self.repository.get_owned(task_id, user_id)
        if task.cancel_requested_at is None:
            task.cancel_requested_at = utc_now()
            task.updated_at = task.cancel_requested_at
            await self.db.flush()
        return task
