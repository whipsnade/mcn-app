import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.tasks.models import AnalysisTask, TaskEvent
from app.tasks.state import TaskStatus
from app.workspace.models import Message, WorkspaceSession


@dataclass
class MemoryExecutionTask:
    id: str = "task-1"
    user_id: str = "user-1"
    session_id: str = "session-1"
    status: str = TaskStatus.PENDING
    plan_json: dict[str, Any] | None = None
    cancel_requested_at: datetime | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    completed_at: datetime | None = None
    error_code: str | None = None


class MemoryExecutionRepository:
    """Task 8 的确定性内存仓储；不触及数据库或真实外部服务。"""

    def __init__(self, task: MemoryExecutionTask | None = None) -> None:
        self.task = task or MemoryExecutionTask()
        self.events: list[str] = []
        self.cancel_count = 0

    async def claim_lease(self, task_id: str, worker_id: str, lease_seconds: int):
        if task_id != self.task.id or self.task.status in {TaskStatus.COMPLETED, TaskStatus.CANCELLED}:
            return None
        self.task.lease_owner = worker_id
        self.task.lease_expires_at = datetime.now(UTC).replace(tzinfo=None)
        if self.task.status == TaskStatus.PENDING:
            self.task.status = TaskStatus.PLANNING
        return self.task

    async def save_plan(self, task_id: str, plan_json: dict[str, Any]) -> None:
        assert task_id == self.task.id
        self.task.plan_json = plan_json
        self.task.status = TaskStatus.RUNNING
        self.events.append("plan.ready")

    async def cancel_requested(self, task_id: str) -> bool:
        assert task_id == self.task.id
        return self.task.cancel_requested_at is not None

    async def mark_cancelled(self, task_id: str, worker_id: str) -> None:
        assert task_id == self.task.id
        self.cancel_count += 1
        self.task.status = TaskStatus.CANCELLED
        self.events.append("task.cancelled")

    async def renew_lease(self, task_id: str, worker_id: str, lease_seconds: int) -> None:
        assert task_id == self.task.id

    async def mark_completed(self, task_id: str, worker_id: str) -> None:
        assert task_id == self.task.id
        self.task.status = TaskStatus.COMPLETED
        self.task.completed_at = datetime.now(UTC).replace(tzinfo=None)
        self.events.append("task.completed")

    async def mark_interrupted(self, task_id: str, worker_id: str) -> None:
        assert task_id == self.task.id
        self.task.status = TaskStatus.INTERRUPTED

    async def mark_failed(self, task_id: str, worker_id: str, code: str) -> None:
        assert task_id == self.task.id
        self.task.status = TaskStatus.FAILED
        self.task.error_code = code

    async def release_lease(self, task_id: str, worker_id: str) -> None:
        assert task_id == self.task.id
        if self.task.lease_owner == worker_id:
            self.task.lease_owner = None

    async def recoverable_task_ids(self) -> tuple[str, ...]:
        if self.task.status in {TaskStatus.PLANNING, TaskStatus.RUNNING, TaskStatus.INTERRUPTED}:
            return (self.task.id,)
        return ()

    async def release_expired_unknown(self, task_id: str, observation_seconds: int) -> bool:
        return False


class FakeExecutionGateway:
    def __init__(self) -> None:
        self.successful_logical_calls = 0
        self.wallet = [1000, 0]
        self._settled: set[str] = set()
        self.started = asyncio.Event()
        self._release = asyncio.Event()
        self._release.set()

    def block(self) -> None:
        self._release.clear()

    def release_success(self) -> None:
        self._release.set()

    async def execute_batch(self, commands):
        self.started.set()
        await self._release.wait()
        rows = []
        for command in commands:
            if command.logical_call_id not in self._settled:
                self._settled.add(command.logical_call_id)
                self.successful_logical_calls += 1
                self.wallet[0] -= 10
            rows.append(type("Result", (), {"status": "settled"})())
        return tuple(rows)


@dataclass
class FakeExecutionScenario:
    repository: MemoryExecutionRepository = field(default_factory=MemoryExecutionRepository)
    gateway: FakeExecutionGateway = field(default_factory=FakeExecutionGateway)
    crash_at: str | None = None

    def __post_init__(self) -> None:
        from app.orchestration.schemas import ToolPlan, ToolPlanStep
        from app.tasks.executor import TaskExecutor

        self.plan = ToolPlan(
            objective="查找达人",
            steps=(
                ToolPlanStep(
                    id="step_1",
                    internal_tool_name="creator.search.v1",
                    arguments={"keyword": "美妆"},
                    evidence_goal="候选结果",
                ),
            ),
        )
        self.executor = TaskExecutor(
            repository=self.repository,
            context_builder=self,
            planner=self,
            gateway=self.gateway,
            worker_id="test-worker",
            checkpoint=self.checkpoint,
        )

    @property
    def task(self) -> MemoryExecutionTask:
        return self.repository.task

    async def build(self, user_id: str, session_id: str):
        return {"user_id": user_id, "session_id": session_id}

    async def plan_for(self, context):
        return self.plan

    async def checkpoint(self, name: str) -> None:
        if name == self.crash_at:
            raise InjectedProcessCrash(name)

    async def reload_task(self) -> MemoryExecutionTask:
        return self.task

    async def wallet_tuple(self) -> tuple[int, int]:
        return tuple(self.gateway.wallet)

    def new_recovery(self):
        from app.tasks.recovery import TaskRecovery

        return TaskRecovery(
            repository=self.repository,
            executor_factory=lambda: self.executor,
            observation_seconds=0,
        )


class InjectedProcessCrash(RuntimeError):
    pass


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


class CommitFailingSession:
    """Delegate real database work, but fail exactly at the commit boundary."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.commit_calls = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self.session, name)

    async def commit(self) -> None:
        self.commit_calls += 1
        await self.session.rollback()
        raise RuntimeError("injected_commit_failure")

    async def rollback(self) -> None:
        await self.session.rollback()


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
