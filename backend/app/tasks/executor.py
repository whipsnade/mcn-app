from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid5

from app.mcp_gateway.service import ExecuteMcpCall
from app.orchestration.batching import build_execution_batches
from app.orchestration.schemas import ToolPlan


class TaskStore(Protocol):
    async def claim_lease(self, task_id: str, worker_id: str, lease_seconds: int) -> Any: ...

    async def save_plan(self, task_id: str, worker_id: str, plan_json: dict[str, Any]) -> bool: ...

    async def cancel_requested(self, task_id: str) -> bool: ...

    async def mark_cancelled(self, task_id: str, worker_id: str) -> None: ...

    async def renew_lease(self, task_id: str, worker_id: str, lease_seconds: int) -> bool: ...

    async def mark_completed(self, task_id: str, worker_id: str) -> None: ...

    async def mark_interrupted(self, task_id: str, worker_id: str) -> None: ...

    async def mark_failed(self, task_id: str, worker_id: str, code: str) -> None: ...

    async def release_lease(self, task_id: str, worker_id: str) -> None: ...


class ContextBuilder(Protocol):
    async def build(self, user_id: str, session_id: str) -> Any: ...


class TaskPlanner(Protocol):
    async def plan(self, context: Any) -> ToolPlan: ...


class McpBatchGateway(Protocol):
    async def execute_batch(self, commands: tuple[ExecuteMcpCall, ...]) -> tuple[Any, ...]: ...


Checkpoint = Callable[[str], Awaitable[None]]


async def _noop_checkpoint(_: str) -> None:
    return None


class TaskExecutor:
    """租约驱动的单任务执行器；断开 SSE 不会影响该协程。"""

    def __init__(
        self,
        *,
        repository: TaskStore,
        context_builder: ContextBuilder,
        planner: TaskPlanner,
        gateway: McpBatchGateway,
        worker_id: str,
        lease_seconds: int = 60,
        heartbeat_seconds: float | None = None,
        checkpoint: Checkpoint = _noop_checkpoint,
    ) -> None:
        self.repository = repository
        self.context_builder = context_builder
        self.planner = planner
        self.gateway = gateway
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = (
            heartbeat_seconds if heartbeat_seconds is not None else lease_seconds / 3
        )
        if self.heartbeat_seconds <= 0 or self.heartbeat_seconds >= lease_seconds:
            raise ValueError("heartbeat_seconds_must_be_less_than_lease_seconds")
        self.checkpoint = checkpoint

    async def run(self, task_id: str) -> None:
        task = await self.repository.claim_lease(task_id, self.worker_id, self.lease_seconds)
        if task is None:
            return
        stop_heartbeat = asyncio.Event()
        lease_lost = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._renew_lease_until_stopped(task.id, stop_heartbeat, lease_lost)
        )
        try:
            plan = await self._load_or_create_plan(task)
            if plan is None or lease_lost.is_set():
                return
            for batch in build_execution_batches(plan):
                if lease_lost.is_set():
                    return
                if await self.repository.cancel_requested(task.id):
                    await self.repository.mark_cancelled(task.id, self.worker_id)
                    return
                # The actual gateway reserves and commits before transport; this
                # deterministic checkpoint models a process loss at that boundary.
                await self.checkpoint("after_reserve")
                commands = tuple(
                    ExecuteMcpCall(
                        logical_call_id=str(uuid5(NAMESPACE_URL, f"{task.id}:{step.id}")),
                        user_id=task.user_id,
                        task_id=task.id,
                        plan_step_id=step.id,
                        internal_tool_name=step.internal_tool_name,
                        arguments=step.arguments,
                    )
                    for step in batch.steps
                )
                rows = await self.gateway.execute_batch(commands)
                await self.checkpoint("after_mcp_result")
                statuses = {getattr(row, "status", None) for row in rows}
                if statuses & {"unknown", "planned", "reserved", "running", "succeeded"}:
                    await self.repository.mark_interrupted(task.id, self.worker_id)
                    return
                if statuses - {"settled"}:
                    await self.repository.mark_failed(task.id, self.worker_id, "mcp_call_failed")
                    return
                await self.checkpoint("after_settle")
                if not await self.repository.renew_lease(
                    task.id, self.worker_id, self.lease_seconds
                ):
                    return
            # Task 9 supplies durable candidate/BI artifacts. Checkpoints make
            # their future persistence boundaries recoverable now.
            await self.checkpoint("after_candidates")
            await self.checkpoint("after_bi")
            await self.repository.mark_completed(task.id, self.worker_id)
        except asyncio.CancelledError:
            await self.repository.mark_interrupted(task.id, self.worker_id)
            raise
        except Exception as error:
            await self.repository.mark_failed(task.id, self.worker_id, type(error).__name__)
        finally:
            stop_heartbeat.set()
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            await self.repository.release_lease(task.id, self.worker_id)

    async def _load_or_create_plan(self, task: Any) -> ToolPlan | None:
        if task.plan_json is not None:
            return ToolPlan.model_validate(task.plan_json)
        context = await self.context_builder.build(task.user_id, task.session_id)
        plan_for = getattr(self.planner, "plan_for", None)
        plan = await plan_for(context) if plan_for is not None else await self.planner.plan(context)
        if not await self.repository.save_plan(
            task.id, self.worker_id, plan.model_dump(mode="json")
        ):
            return None
        return plan

    async def _renew_lease_until_stopped(
        self, task_id: str, stop: asyncio.Event, lease_lost: asyncio.Event
    ) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.heartbeat_seconds)
            except TimeoutError:
                if not await self.repository.renew_lease(
                    task_id, self.worker_id, self.lease_seconds
                ):
                    lease_lost.set()
                    return


class TaskRunner:
    """持有强引用的进程内 runner，避免 create_task 被垃圾回收。"""

    def __init__(self, executor_factory: Callable[[], TaskExecutor]) -> None:
        self._executor_factory = executor_factory
        self._tasks: set[asyncio.Task[None]] = set()
        self._active_task_ids: set[str] = set()
        self._accepting = True

    def submit(self, task_id: str) -> None:
        if not self._accepting or task_id in self._active_task_ids:
            return
        running = asyncio.create_task(self._executor_factory().run(task_id))
        self._tasks.add(running)
        self._active_task_ids.add(task_id)

        def discard(completed: asyncio.Task[None]) -> None:
            self._tasks.discard(completed)
            self._active_task_ids.discard(task_id)

        running.add_done_callback(discard)

    async def shutdown(self, *, timeout_seconds: float = 5) -> None:
        self._accepting = False
        if not self._tasks:
            return
        _, pending = await asyncio.wait(self._tasks, timeout=timeout_seconds)
        for running in pending:
            running.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
