import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator, Callable, Coroutine
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.tasks.models import AnalysisTask, TaskEvent
from app.tasks.state import TaskStatus
from app.workspace.models import Message, WorkspaceSession


class MemoryTaskEventRepository:
    def __init__(self) -> None:
        self.events: list[TaskEvent] = []

    async def append_event(
        self,
        task_id: str,
        user_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> TaskEvent:
        event = TaskEvent(
            id=len(self.events) + 1,
            task_id=task_id,
            user_id=user_id,
            event_type=event_type,
            payload_json=payload,
            created_at=datetime.now(UTC).replace(tzinfo=None),
        )
        self.events.append(event)
        return event

    async def list_events_after(self, task_id: str, last_event_id: int) -> list[TaskEvent]:
        return [
            event
            for event in self.events
            if event.task_id == task_id and event.id > last_event_id
        ]

    async def list_owned_events_after(
        self, task_id: str, user_id: str, last_event_id: int
    ) -> list[TaskEvent]:
        return [
            event
            for event in await self.list_events_after(task_id, last_event_id)
            if event.user_id == user_id
        ]


class MemoryTaskEventBroker:
    def __init__(self) -> None:
        self.subscriptions: dict[str, list[asyncio.Queue[TaskEvent]]] = defaultdict(list)

    async def subscribe(self, task_id: str) -> asyncio.Queue[TaskEvent]:
        queue: asyncio.Queue[TaskEvent] = asyncio.Queue()
        self.subscriptions[task_id].append(queue)
        return queue

    async def unsubscribe(self, task_id: str, queue: asyncio.Queue[TaskEvent]) -> None:
        self.subscriptions[task_id].remove(queue)
        if not self.subscriptions[task_id]:
            del self.subscriptions[task_id]

    async def publish(self, event: TaskEvent) -> None:
        for queue in list(self.subscriptions.get(event.task_id, [])):
            await queue.put(event)


class _NoOpSessionContext:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: Any) -> None:
        return None


class NoOpSessionFactory:
    def __call__(self) -> _NoOpSessionContext:
        return _NoOpSessionContext()

    def begin(self) -> _NoOpSessionContext:
        return _NoOpSessionContext()


@pytest_asyncio.fixture(name="workspace_factory")
async def workspace_factory_fixture(
    db_session: AsyncSession,
) -> Callable[[str], Coroutine[Any, Any, WorkspaceSession]]:
    async def create_workspace(user_id: str) -> WorkspaceSession:
        now = datetime.now(UTC).replace(tzinfo=None)
        workspace = WorkspaceSession(
            id=str(uuid4()),
            user_id=user_id,
            title="测试会话",
            brand="测试品牌",
            campaign_name="测试活动",
            status="draft",
            platforms=["bilibili"],
            category="科技",
            target_audience="科技兴趣用户",
            budget_min=None,
            budget_max=None,
            filters_snapshot={},
            is_starred=False,
            last_accessed_at=now,
            created_at=now,
            updated_at=now,
        )
        db_session.add(workspace)
        await db_session.flush()
        return workspace

    return create_workspace


@pytest_asyncio.fixture(name="persisted_task")
async def persisted_task_fixture(
    db_session: AsyncSession, user_factory, workspace_factory
) -> AnalysisTask:
    user = await user_factory()
    workspace = await workspace_factory(user.id)
    now = datetime.now(UTC).replace(tzinfo=None)
    message = Message(
        id=str(uuid4()),
        session_id=workspace.id,
        user_id=user.id,
        role="user",
        content="测试任务",
        sequence=1,
        metadata_json={},
        created_at=now,
    )
    db_session.add(message)
    await db_session.flush()
    task = AnalysisTask(
        id=str(uuid4()),
        user_id=user.id,
        session_id=workspace.id,
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
    db_session.add(task)
    await db_session.flush()
    return task


@pytest_asyncio.fixture(name="task_event_stream")
async def task_event_stream_fixture():
    from app.tasks.events import TaskEventStream

    repository = MemoryTaskEventRepository()
    broker = MemoryTaskEventBroker()
    stream = TaskEventStream(NoOpSessionFactory(), lambda _: repository, broker)
    stream.repository = repository
    stream.broker = broker
    return stream


async def collect_event_ids(events: AsyncIterator[Any], *, count: int) -> list[int]:
    ids: list[int] = []
    async for event in events:
        ids.append(event.id)
        if len(ids) == count:
            await events.aclose()
            return ids
    return ids
