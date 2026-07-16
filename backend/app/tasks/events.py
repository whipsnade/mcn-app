import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.tasks.models import AnalysisTask, TaskEvent
from app.tasks.repository import TaskRepository
from app.workspace.models import Message
from sqlalchemy import select


class TaskEventBroker:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[TaskEvent]]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def subscribe(self, task_id: str) -> asyncio.Queue[TaskEvent]:
        queue: asyncio.Queue[TaskEvent] = asyncio.Queue()
        async with self._lock:
            self._subscribers[task_id].add(queue)
        return queue

    async def unsubscribe(self, task_id: str, queue: asyncio.Queue[TaskEvent]) -> None:
        async with self._lock:
            subscribers = self._subscribers.get(task_id)
            if subscribers is None:
                return
            subscribers.discard(queue)
            if not subscribers:
                del self._subscribers[task_id]

    async def publish(self, event: TaskEvent) -> None:
        async with self._lock:
            subscribers = tuple(self._subscribers.get(event.task_id, ()))
        for queue in subscribers:
            await queue.put(event)


class TaskEventStream:
    def __init__(
        self,
        session_factory,
        repository_factory: Callable[[AsyncSession], TaskRepository],
        broker: TaskEventBroker,
    ) -> None:
        self.session_factory = session_factory
        self.repository_factory = repository_factory
        self.broker = broker

    async def append(
        self, task: AnalysisTask, event_type: str, payload: dict[str, Any]
    ) -> TaskEvent:
        async with self.session_factory.begin() as db:
            event = await self.repository_factory(db).append_event(
                task.id, task.user_id, event_type, payload
            )
        await self.broker.publish(event)
        return event

    async def stream(
        self, task_id: str, user_id: str, last_event_id: int
    ) -> AsyncIterator[TaskEvent]:
        terminal_events = {
            "task.completed",
            "task.completed_with_warnings",
            "task.failed",
            "task.cancelled",
        }
        queue = await self.broker.subscribe(task_id)
        seen = last_event_id
        try:
            async with self.session_factory() as db:
                rows = await self.repository_factory(db).list_owned_events_after(
                    task_id, user_id, seen
                )
            for row in rows:
                if row.id > seen:
                    seen = row.id
                    yield row
                    if row.event_type in terminal_events:
                        if not await self._followup_pending(task_id):
                            return
            while True:
                try:
                    row = await asyncio.wait_for(queue.get(), timeout=0.5)
                    if row.user_id == user_id and row.id > seen:
                        seen = row.id
                        yield row
                        if row.event_type in terminal_events:
                            if not await self._followup_pending(task_id):
                                return
                except TimeoutError:
                    # Most task boundaries are written through repositories that
                    # do not share this process' broker. The database is the
                    # authoritative notification channel and closes that gap.
                    async with self.session_factory() as db:
                        rows = await self.repository_factory(db).list_owned_events_after(
                            task_id, user_id, seen
                        )
                    for row in rows:
                        if row.id > seen:
                            seen = row.id
                            yield row
                            if row.event_type in terminal_events:
                                if not await self._followup_pending(task_id):
                                    return
        finally:
            await self.broker.unsubscribe(task_id, queue)

    async def _followup_pending(self, task_id: str) -> bool:
        """Keep a newly completed stream open until follow-up generation settles."""
        async with self.session_factory() as db:
            rows = list(
                (
                    await db.scalars(select(Message).where(Message.role == "assistant"))
                ).all()
            )
            return any(
                message.metadata_json.get("task_id") == task_id
                and
                message.metadata_json.get("followup_suggestions_status") == "pending"
                for message in rows
            )
