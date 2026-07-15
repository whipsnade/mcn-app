import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.tasks.models import AnalysisTask, TaskEvent
from app.tasks.repository import TaskRepository


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
            while True:
                row = await queue.get()
                if row.user_id == user_id and row.id > seen:
                    seen = row.id
                    yield row
        finally:
            await self.broker.unsubscribe(task_id, queue)
