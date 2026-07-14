from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.tasks.models import AnalysisTask, TaskEvent


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class TaskRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_owned(self, task_id: str, user_id: str) -> AnalysisTask:
        task = await self.db.scalar(
            select(AnalysisTask).where(
                AnalysisTask.id == task_id,
                AnalysisTask.user_id == user_id,
            )
        )
        if task is None:
            raise LookupError("task_not_found")
        return task

    async def append_event(
        self,
        task_id: str,
        user_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> TaskEvent:
        event = TaskEvent(
            task_id=task_id,
            user_id=user_id,
            event_type=event_type,
            payload_json=payload,
            created_at=utc_now(),
        )
        self.db.add(event)
        await self.db.flush()
        return event

    async def list_events_after(self, task_id: str, last_event_id: int) -> list[TaskEvent]:
        statement = (
            select(TaskEvent)
            .where(TaskEvent.task_id == task_id, TaskEvent.id > last_event_id)
            .order_by(TaskEvent.id.asc())
        )
        return list((await self.db.scalars(statement)).all())

    async def list_owned_events_after(
        self, task_id: str, user_id: str, last_event_id: int
    ) -> list[TaskEvent]:
        await self.get_owned(task_id, user_id)
        return await self.list_events_after(task_id, last_event_id)
