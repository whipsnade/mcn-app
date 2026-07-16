from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid5

from app.mcp_gateway.service import ExecuteMcpCall
from app.model.contracts import ModelAdapterError
from app.orchestration.batching import build_execution_batches
from app.orchestration.schemas import PlanValidationError, ReplanContext, ReplanFailure, ToolPlan
from app.tasks.errors import canonical_platform, safe_error
from app.tasks.state import TaskEventType


class TaskStore(Protocol):
    async def claim_lease(self, task_id: str, worker_id: str, lease_seconds: int) -> Any: ...

    async def save_plan(self, task_id: str, worker_id: str, plan_json: dict[str, Any]) -> bool: ...

    async def save_replan(self, task_id: str, worker_id: str, replan_json: dict[str, Any]) -> bool: ...

    async def cancel_requested(self, task_id: str) -> bool: ...

    async def mark_cancelled(self, task_id: str, worker_id: str) -> None: ...

    async def renew_lease(self, task_id: str, worker_id: str, lease_seconds: int) -> bool: ...

    async def mark_completed(self, task_id: str, worker_id: str) -> None: ...

    async def mark_completed_with_warnings(
        self, task_id: str, worker_id: str, warning_code: str, warning_message: str | None = None
    ) -> None: ...

    async def mark_interrupted(self, task_id: str, worker_id: str) -> None: ...

    async def mark_failed(
        self, task_id: str, worker_id: str, code: str, message: str | None = None
    ) -> None: ...

    async def append_event(
        self, task_id: str, user_id: str, event_type: str, payload: dict[str, Any]
    ) -> Any: ...

    async def release_lease(self, task_id: str, worker_id: str) -> None: ...


class ContextBuilder(Protocol):
    async def build(self, user_id: str, session_id: str) -> Any: ...


class TaskPlanner(Protocol):
    async def plan(self, context: Any) -> ToolPlan: ...


class McpBatchGateway(Protocol):
    async def execute_batch(self, commands: tuple[ExecuteMcpCall, ...]) -> tuple[Any, ...]: ...


class TaskArtifacts(Protocol):
    async def build_candidates(self, task_id: str) -> Any: ...

    async def build_bi_report(self, task_id: str) -> Any: ...

    async def stream_summary(self, task_id: str) -> Any: ...


Checkpoint = Callable[[str], Awaitable[None]]


async def _noop_checkpoint(_: str) -> None:
    return None


def build_tool_event_payload(
    internal_tool_name: str,
    *,
    status: str,
    step_index: int,
    step_total: int,
    error_code: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "platform": canonical_platform(internal_tool_name),
        "step_index": step_index,
        "step_total": step_total,
    }
    if status in {"failed", "unknown"}:
        failure = safe_error(error_code)
        payload.update({"error_code": failure.code, "message": failure.message})
    return payload


def aggregate_mcp_progress(commands: tuple[Any, ...], rows: tuple[Any, ...]) -> dict[str, object]:
    platforms: dict[str, dict[str, int]] = {}
    for command in commands:
        label = canonical_platform(getattr(command, "internal_tool_name", None))
        platforms.setdefault(label, {"succeeded": 0, "failed": 0, "unknown": 0})
    for row in rows:
        label = canonical_platform(getattr(row, "internal_tool_name", None))
        counts = platforms.setdefault(label, {"succeeded": 0, "failed": 0, "unknown": 0})
        status = getattr(row, "status", None)
        if status in {"settled", "succeeded"}:
            counts["succeeded"] += 1
        elif status in {"unknown"}:
            counts["unknown"] += 1
        else:
            counts["failed"] += 1
    return {"step_total": len(commands), "step_index": len(rows), "platforms": platforms}


class TaskExecutor:
    """租约驱动的单任务执行器；断开 SSE 不会影响该协程。"""

    def __init__(
        self,
        *,
        repository: TaskStore,
        context_builder: ContextBuilder,
        planner: TaskPlanner,
        gateway: McpBatchGateway,
        artifacts: TaskArtifacts | None = None,
        worker_id: str,
        lease_seconds: int = 60,
        heartbeat_seconds: float | None = None,
        checkpoint: Checkpoint = _noop_checkpoint,
    ) -> None:
        self.repository = repository
        self.context_builder = context_builder
        self.planner = planner
        self.gateway = gateway
        self.artifacts = artifacts
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
            has_settled = False
            partial_failure = False
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
                        lease_owner=self.worker_id,
                    )
                    for step in batch.steps
                )
                for index, command in enumerate(commands, start=1):
                    await self.repository.append_event(
                        task.id,
                        task.user_id,
                        TaskEventType.TOOL_STARTED,
                        build_tool_event_payload(
                            command.internal_tool_name,
                            status="started",
                            step_index=index,
                            step_total=len(commands),
                        ),
                    )
                rows = await self.gateway.execute_batch(commands)
                progress = aggregate_mcp_progress(commands, rows)
                for index, row in enumerate(rows, start=1):
                    row_status = getattr(row, "status", None)
                    event_type = (
                        TaskEventType.TOOL_SUCCEEDED
                        if row_status in {"settled", "succeeded"}
                        else TaskEventType.TOOL_UNKNOWN
                        if row_status == "unknown"
                        else TaskEventType.TOOL_FAILED
                    )
                    command_name = (
                        getattr(row, "internal_tool_name", None)
                        or commands[min(index - 1, len(commands) - 1)].internal_tool_name
                    )
                    await self.repository.append_event(
                        task.id,
                        task.user_id,
                        event_type,
                        {
                            **build_tool_event_payload(
                                command_name,
                                status=(
                                    "succeeded"
                                    if event_type == TaskEventType.TOOL_SUCCEEDED
                                    else "unknown"
                                    if event_type == TaskEventType.TOOL_UNKNOWN
                                    else "failed"
                                ),
                                step_index=index,
                                step_total=len(commands),
                                error_code=getattr(row, "error_type", None),
                            ),
                            "platform_progress": progress["platforms"],
                        },
                    )
                await self.checkpoint("after_mcp_result")
                statuses = {getattr(row, "status", None) for row in rows}
                if statuses & {"unknown", "planned", "reserved", "running", "succeeded"}:
                    await self.repository.mark_interrupted(task.id, self.worker_id)
                    return
                if statuses - {"settled"}:
                    has_settled = has_settled or "settled" in statuses
                    supplement = await self._load_or_create_replan(task, plan, rows)
                    if supplement is not None:
                        supplement_commands = self._commands(task, supplement, revision="replan")
                        for index, command in enumerate(supplement_commands, start=1):
                            await self.repository.append_event(
                                task.id,
                                task.user_id,
                                TaskEventType.TOOL_STARTED,
                                build_tool_event_payload(
                                    command.internal_tool_name,
                                    status="started",
                                    step_index=index,
                                    step_total=len(supplement_commands),
                                ),
                            )
                        supplement_rows = await self.gateway.execute_batch(supplement_commands)
                        supplement_progress = aggregate_mcp_progress(
                            supplement_commands, supplement_rows
                        )
                        for index, row in enumerate(supplement_rows, start=1):
                            row_status = getattr(row, "status", None)
                            event_type = (
                                TaskEventType.TOOL_SUCCEEDED
                                if row_status in {"settled", "succeeded"}
                                else TaskEventType.TOOL_UNKNOWN
                                if row_status == "unknown"
                                else TaskEventType.TOOL_FAILED
                            )
                            command_name = (
                                getattr(row, "internal_tool_name", None)
                                or supplement_commands[
                                    min(index - 1, len(supplement_commands) - 1)
                                ].internal_tool_name
                            )
                            await self.repository.append_event(
                                task.id,
                                task.user_id,
                                event_type,
                                {
                                    **build_tool_event_payload(
                                        command_name,
                                        status=(
                                            "succeeded"
                                            if event_type == TaskEventType.TOOL_SUCCEEDED
                                            else "unknown"
                                            if event_type == TaskEventType.TOOL_UNKNOWN
                                            else "failed"
                                        ),
                                        step_index=index,
                                        step_total=len(supplement_commands),
                                        error_code=getattr(row, "error_type", None),
                                    ),
                                    "platform_progress": supplement_progress["platforms"],
                                },
                            )
                        supplement_statuses = {getattr(row, "status", None) for row in supplement_rows}
                        if supplement_statuses & {"unknown", "planned", "reserved", "running", "succeeded"}:
                            await self.repository.mark_interrupted(task.id, self.worker_id)
                            return
                        has_settled = has_settled or "settled" in supplement_statuses
                        partial_failure = bool(supplement_statuses - {"settled"})
                    else:
                        partial_failure = True
                    break
                has_settled = True
                await self.checkpoint("after_settle")
                if not await self.repository.renew_lease(
                    task.id, self.worker_id, self.lease_seconds
                ):
                    return
            if partial_failure and not has_settled:
                await self.repository.mark_failed(task.id, self.worker_id, "mcp_call_failed")
                return
            if self.artifacts is not None:
                await self.artifacts.build_candidates(task.id)
            await self.checkpoint("after_candidates")
            if self.artifacts is not None:
                await self.artifacts.build_bi_report(task.id)
            await self.checkpoint("after_bi")
            if self.artifacts is not None:
                await self.artifacts.stream_summary(task.id)
            if partial_failure:
                await self.repository.mark_completed_with_warnings(
                    task.id, self.worker_id, "mcp_partial_failure", "部分社媒渠道查询失败，已保留可用结果。"
                )
            else:
                await self.repository.mark_completed(task.id, self.worker_id)
            # The task terminal event is durable before suggestion generation
            # starts; suggestion failures can therefore never roll it back.
            prepare_followups = getattr(self.artifacts, "prepare_followups", None)
            if prepare_followups is not None:
                try:
                    await prepare_followups(task.id)
                except Exception:
                    pass
            generate_followups = getattr(self.artifacts, "generate_followups", None)
            if generate_followups is not None:
                try:
                    await generate_followups(task.id)
                except Exception:
                    # Follow-up generation is non-fatal by design.
                    pass
        except asyncio.CancelledError:
            await self.repository.mark_interrupted(task.id, self.worker_id)
            raise
        except Exception as error:
            code = (
                error.code
                if isinstance(error, (ModelAdapterError, PlanValidationError))
                else type(error).__name__
            )
            await self.repository.mark_failed(task.id, self.worker_id, code)
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

    async def _load_or_create_replan(
        self, task: Any, plan: ToolPlan, rows: tuple[Any, ...]
    ) -> ToolPlan | None:
        saved = getattr(task, "replan_json", None)
        if saved is not None:
            return ToolPlan.model_validate(saved)
        replan_for = getattr(self.planner, "replan", None)
        if replan_for is None:
            return None
        remaining_calls = max(0, int(getattr(task, "max_calls", 10)) - len(plan.steps))
        if remaining_calls == 0:
            return None
        failures = tuple(
            ReplanFailure(
                step_id=str(getattr(row, "plan_step_id", "step_0")),
                internal_tool_name=str(getattr(row, "internal_tool_name", "unknown")),
                error_code=str(getattr(row, "error_type", "mcp_call_failed")),
                diagnostic=(getattr(row, "evidence_json", {}) or {}).get("output_validation_diagnostic"),
            )
            for row in rows
            if getattr(row, "status", None) != "settled"
        )
        if not failures:
            return None
        context = await self.context_builder.build(task.user_id, task.session_id)
        supplement = await replan_for(
            context,
            ReplanContext(
                completed_step_ids=tuple(
                    str(getattr(row, "plan_step_id", ""))
                    for row in rows
                    if getattr(row, "status", None) == "settled"
                ),
                failed_steps=failures,
                remaining_calls=remaining_calls,
                remaining_points=remaining_calls * 10,
            ),
        )
        if not await self.repository.save_replan(
            task.id, self.worker_id, supplement.model_dump(mode="json")
        ):
            return None
        return supplement

    def _commands(self, task: Any, plan: ToolPlan, *, revision: str = "plan") -> tuple[ExecuteMcpCall, ...]:
        return tuple(
            ExecuteMcpCall(
                logical_call_id=str(uuid5(NAMESPACE_URL, f"{task.id}:{revision}:{step.id}")),
                user_id=task.user_id,
                task_id=task.id,
                plan_step_id=step.id,
                internal_tool_name=step.internal_tool_name,
                arguments=step.arguments,
                lease_owner=self.worker_id,
            )
            for step in plan.steps
        )

    async def _renew_lease_until_stopped(
        self, task_id: str, stop: asyncio.Event, lease_lost: asyncio.Event
    ) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.heartbeat_seconds)
            except TimeoutError:
                try:
                    renewed = await self.repository.renew_lease(
                        task_id, self.worker_id, self.lease_seconds
                    )
                except Exception:
                    lease_lost.set()
                    return
                if not renewed:
                    lease_lost.set()
                    return


class TaskRunner:
    """持有强引用的进程内 runner，避免 create_task 被垃圾回收。"""

    def __init__(
        self,
        executor_factory: Callable[[], TaskExecutor],
        *,
        followup_preparer: Callable[[str], Awaitable[bool]] | None = None,
        followup_generator: Callable[[str], Awaitable[bool]] | None = None,
    ) -> None:
        self._executor_factory = executor_factory
        self._followup_preparer = followup_preparer
        self._followup_generator = followup_generator
        self._tasks: set[asyncio.Task[None]] = set()
        self._active_task_ids: set[str] = set()
        self._active_followup_ids: set[str] = set()
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

    async def retry_followup(self, task_id: str) -> bool:
        """Requeue only the suggestion generation for an existing terminal task."""
        if not self._accepting or self._followup_preparer is None or self._followup_generator is None:
            return False
        if task_id in self._active_followup_ids:
            return False
        prepared = await self._followup_preparer(task_id)
        if not prepared:
            return False
        self._active_followup_ids.add(task_id)

        async def generate() -> None:
            try:
                await self._followup_generator(task_id)
            finally:
                self._active_followup_ids.discard(task_id)

        running = asyncio.create_task(generate())
        self._tasks.add(running)
        running.add_done_callback(self._tasks.discard)
        return True

    async def shutdown(self, *, timeout_seconds: float = 5) -> None:
        self._accepting = False
        if not self._tasks:
            return
        _, pending = await asyncio.wait(self._tasks, timeout=timeout_seconds)
        for running in pending:
            running.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
