from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid5

from app.mcp_gateway.service import ExecuteMcpCall
from app.model.contracts import ModelAdapterError
from app.orchestration.batching import build_execution_batches
from app.orchestration.loop import (
    AgentLoopContext,
    EvidenceNote,
    TrajectoryStep,
    resolve_agent_call,
    restore_agent_trajectory,
)
from app.orchestration.schemas import PlanValidationError, ReplanContext, ReplanFailure, ToolPlan
from app.reporting.analysis_reports import sanitize_evidence
from app.tasks.errors import canonical_platform, safe_error
from app.tasks.state import TaskEventType


class TaskStore(Protocol):
    async def claim_lease(self, task_id: str, worker_id: str, lease_seconds: int) -> Any: ...

    async def save_plan(self, task_id: str, worker_id: str, plan_json: dict[str, Any]) -> bool: ...

    async def save_trajectory(
        self, task_id: str, worker_id: str, trajectory_json: dict[str, Any]
    ) -> bool: ...

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

    async def build_agent_context(self, user_id: str, session_id: str) -> AgentLoopContext: ...


class TaskPlanner(Protocol):
    async def plan(self, context: Any) -> ToolPlan: ...

    async def agent_decide(self, context: AgentLoopContext) -> Any: ...


class McpBatchGateway(Protocol):
    async def execute_batch(self, commands: tuple[ExecuteMcpCall, ...]) -> tuple[Any, ...]: ...


class TaskArtifacts(Protocol):
    async def build_candidates(self, task_id: str) -> Any: ...

    async def build_bi_report(self, task_id: str) -> Any: ...

    async def build_analysis_report(self, task_id: str) -> Any: ...

    async def stream_summary(self, task_id: str) -> Any: ...

    async def stream_analysis_summary(self, task_id: str) -> Any: ...


Checkpoint = Callable[[str], Awaitable[None]]
logger = logging.getLogger(__name__)

# A failed/released MCP call did not produce a billable successful response,
# so it may be replaced by one recovery step.  An ``unknown`` outcome is
# deliberately excluded: replaying a possibly-sent request could duplicate a
# non-idempotent upstream operation.
_REPLAN_RETRYABLE_STATUSES = frozenset({"failed", "released"})


async def _noop_checkpoint(_: str) -> None:
    return None


def build_tool_event_payload(
    internal_tool_name: str,
    *,
    status: str,
    step_index: int,
    step_total: int,
    error_code: str | None = None,
    evidence_kind: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "platform": canonical_platform(internal_tool_name),
        "step_index": step_index,
        "step_total": step_total,
    }
    if evidence_kind in {"brand", "kol"}:
        payload["evidence_kind"] = evidence_kind
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


def replan_retry_budget(rows: tuple[Any, ...], max_calls: int = 10) -> int:
    """Return the number of safe replacement calls available for a batch.

    The old implementation used ``max_calls - len(plan.steps)``.  That made a
    ten-step plan unrecoverable even when several upstream calls failed and
    therefore did not consume points.  A replacement is allowed only for a
    terminal failed/released row; unknown outcomes remain non-replayable.
    """
    if max_calls <= 0:
        return 0
    failed_count = sum(
        1 for row in rows if getattr(row, "status", None) in _REPLAN_RETRYABLE_STATUSES
    )
    return min(max_calls, failed_count)


def summarize_mcp_failures(rows: tuple[Any, ...], *, limit: int = 5) -> str:
    """Build a user-safe failure summary without tool names or raw payloads."""
    summaries: list[str] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        status = getattr(row, "status", None)
        if status in {"settled", "succeeded"}:
            continue
        platform = canonical_platform(getattr(row, "internal_tool_name", None))
        code = getattr(row, "error_type", None)
        if status == "unknown":
            code = "mcp_unknown_outcome"
        failure = safe_error(code or "mcp_call_failed")
        key = (platform, failure.code)
        if key in seen:
            continue
        seen.add(key)
        summaries.append(f"{platform}：{failure.message}")
        if len(summaries) >= limit:
            break
    return "；".join(summaries)


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
            if getattr(task, "kind", "pipeline") == "agent":
                await self._run_agent_loop(task)
                return
            plan = await self._load_or_create_plan(task)
            if plan is None or lease_lost.is_set():
                return
            has_settled = False
            partial_failure = False
            unresolved_failures: tuple[Any, ...] = ()
            completed_step_ids: set[str] = set()
            replan_failed = False
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
                step_kinds = {step.id: step.evidence_kind for step in batch.steps}
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
                            evidence_kind=step_kinds.get(command.plan_step_id),
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
                                evidence_kind=step_kinds.get(
                                    getattr(row, "plan_step_id", None)
                                    or commands[min(index - 1, len(commands) - 1)].plan_step_id
                                ),
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
                    completed_step_ids.update(
                        str(getattr(row, "plan_step_id", ""))
                        for row in rows
                        if getattr(row, "status", None) in {"settled", "succeeded"}
                    )
                    non_retryable_failures = tuple(
                        row
                        for row in rows
                        if getattr(row, "status", None) not in {"settled", "succeeded"}
                        and getattr(row, "status", None) not in _REPLAN_RETRYABLE_STATUSES
                    )
                    try:
                        supplement = await self._load_or_create_replan(
                            task,
                            plan,
                            rows,
                            completed_step_ids=tuple(sorted(completed_step_ids)),
                        )
                    except Exception:
                        # A failed recovery plan must not discard already-settled
                        # evidence.  The outer task status decides whether a
                        # partial BI report can still be produced.
                        replan_failed = True
                        logger.exception(
                            "task replanning failed task_id=%s", task.id
                        )
                        supplement = None
                    if supplement is not None:
                        supplement_commands = self._commands(task, supplement, revision="replan")
                        supplement_step_kinds = {
                            step.id: step.evidence_kind for step in supplement.steps
                        }
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
                                    evidence_kind=supplement_step_kinds.get(command.plan_step_id),
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
                                        evidence_kind=supplement_step_kinds.get(
                                            getattr(row, "plan_step_id", None)
                                            or supplement_commands[
                                                min(index - 1, len(supplement_commands) - 1)
                                            ].plan_step_id
                                        ),
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
                        unresolved_failures = non_retryable_failures + tuple(
                            row
                            for row in supplement_rows
                            if getattr(row, "status", None) not in {"settled", "succeeded"}
                        )
                    else:
                        partial_failure = True
                        unresolved_failures = tuple(
                            row
                            for row in rows
                            if getattr(row, "status", None) not in {"settled", "succeeded"}
                        )
                    break
                has_settled = True
                completed_step_ids.update(step.id for step in batch.steps)
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
            report_warnings: list[str] = []
            if self.artifacts is not None:
                report = await self.artifacts.build_bi_report(task.id)
                chart_data = getattr(report, "chart_data_json", {}) or {}
                report_warnings = [
                    str(item) for item in chart_data.get("warnings", []) if str(item).strip()
                ]
            await self.checkpoint("after_bi")
            if self.artifacts is not None:
                await self.artifacts.stream_summary(task.id)
            if partial_failure or report_warnings:
                failure_summary = summarize_mcp_failures(unresolved_failures)
                warning_parts = list(report_warnings)
                if replan_failed:
                    warning_parts.append("自动重新规划未完成，已保留可用结果。")
                if failure_summary:
                    warning_parts.append(failure_summary)
                warning_message = (
                    "；".join(warning_parts)
                    if warning_parts
                    else "部分社媒渠道查询失败，已保留可用结果。"
                )
                await self.repository.mark_completed_with_warnings(
                    task.id,
                    self.worker_id,
                    "mcp_partial_failure" if partial_failure else "report_data_unavailable",
                    warning_message,
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
            # Keep the user-facing error sanitized, but leave a safe server
            # traceback so a failed planning/MCP boundary is diagnosable.
            logger.exception(
                "task execution failed task_id=%s error_type=%s error_code=%s",
                task.id,
                type(error).__name__,
                code,
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

    async def _run_agent_loop(self, task: Any) -> None:
        """迭代式工具调用循环：每轮由模型决定下一步，产出版本化自由报告。"""
        build_agent_context = getattr(self.context_builder, "build_agent_context", None)
        decide = getattr(self.planner, "agent_decide", None)
        if build_agent_context is None or decide is None:
            raise PlanValidationError("AGENT_RUNTIME_UNAVAILABLE")
        max_calls = max(1, int(getattr(task, "max_calls", 10) or 10))
        context = await build_agent_context(task.user_id, task.session_id)
        trajectory = restore_agent_trajectory(getattr(task, "plan_json", None))
        if getattr(task, "plan_json", None) is None:
            # First run: emit plan.ready once so clients leave the planning phase.
            if not await self.repository.save_plan(
                task.id, self.worker_id, trajectory.as_plan_json()
            ):
                return
        feedback: list[EvidenceNote] = []
        invalid_streak = 0
        while len(trajectory.results) < max_calls:
            if await self.repository.cancel_requested(task.id):
                await self.repository.mark_cancelled(task.id, self.worker_id)
                return
            # A persisted step without a result is replayed with its original
            # arguments (crash between prepare and finalize); only when no
            # pending step exists do we ask the model for the next move.
            pending = (
                trajectory.steps[len(trajectory.results)]
                if len(trajectory.steps) > len(trajectory.results)
                else None
            )
            if pending is None:
                round_context = context.model_copy(
                    update={
                        "notes": (*trajectory.results, *feedback),
                        "remaining_calls": max_calls - len(trajectory.results),
                    }
                )
                decision = await decide(round_context)
                if decision.action == "finish":
                    break
                # 工具/渠道校验、参数归一化（平台别名、默认三个月时间窗回填、
                # 时间窗钳制、keyword 必填 name）与 Schema 校验一次完成；
                # 持久化与实发都使用归一化后的参数。
                try:
                    _tool, normalized_arguments = resolve_agent_call(decision, context)
                except PlanValidationError as error:
                    invalid_streak += 1
                    if invalid_streak >= 2:
                        raise
                    feedback.append(
                        EvidenceNote(
                            step_id="invalid",
                            tool=decision.internal_tool_name or "unknown",
                            status="failed",
                            summary=(
                                f"上一次决策未通过校验（{error.code}），"
                                "请在已审核工具与授权渠道内重新选择。"
                            ),
                        )
                    )
                    continue
                invalid_streak = 0
                pending = TrajectoryStep(
                    id=f"step_{len(trajectory.results) + 1}",
                    internal_tool_name=decision.internal_tool_name or "",
                    arguments=normalized_arguments,
                    evidence_goal=decision.evidence_goal,
                )
                trajectory.steps.append(pending)
                # Persist BEFORE invoking so the gateway's arguments_loader can
                # reload byte-identical arguments after a crash.
                if not await self.repository.save_trajectory(
                    task.id, self.worker_id, trajectory.as_plan_json()
                ):
                    return
            step_index = len(trajectory.results) + 1
            await self.repository.append_event(
                task.id,
                task.user_id,
                TaskEventType.TOOL_STARTED,
                build_tool_event_payload(
                    pending.internal_tool_name,
                    status="started",
                    step_index=step_index,
                    step_total=max_calls,
                ),
            )
            command = ExecuteMcpCall(
                logical_call_id=str(uuid5(NAMESPACE_URL, f"{task.id}:{pending.id}")),
                user_id=task.user_id,
                task_id=task.id,
                plan_step_id=pending.id,
                internal_tool_name=pending.internal_tool_name,
                arguments=pending.arguments,
                lease_owner=self.worker_id,
            )
            await self.checkpoint("after_reserve")
            rows = await self.gateway.execute_batch((command,))
            row = rows[0] if rows else None
            row_status = getattr(row, "status", None)
            event_type = (
                TaskEventType.TOOL_SUCCEEDED
                if row_status in {"settled", "succeeded"}
                else TaskEventType.TOOL_UNKNOWN
                if row_status == "unknown"
                else TaskEventType.TOOL_FAILED
            )
            await self.repository.append_event(
                task.id,
                task.user_id,
                event_type,
                build_tool_event_payload(
                    pending.internal_tool_name,
                    status=(
                        "succeeded"
                        if event_type == TaskEventType.TOOL_SUCCEEDED
                        else "unknown"
                        if event_type == TaskEventType.TOOL_UNKNOWN
                        else "failed"
                    ),
                    step_index=step_index,
                    step_total=max_calls,
                    error_code=getattr(row, "error_type", None),
                ),
            )
            await self.checkpoint("after_mcp_result")
            if row_status in {"unknown", "planned", "reserved", "running", "succeeded"}:
                # Same semantics as the pipeline: possibly-sent calls are never
                # replayed in this run; recovery reconciles them later.
                await self.repository.mark_interrupted(task.id, self.worker_id)
                return
            if row_status == "settled":
                trajectory.results.append(
                    EvidenceNote(
                        step_id=pending.id,
                        tool=pending.internal_tool_name,
                        status="settled",
                        summary=sanitize_evidence(
                            (getattr(row, "evidence_json", None) or {}).get("structured_content")
                        ),
                    )
                )
            else:
                failure = safe_error(getattr(row, "error_type", None) or "mcp_call_failed")
                # 上游业务错误原文（如“标签不在列表中，建议先用 match_best_tag”）
                # 是模型自我纠正最关键的信号，一并回喂。
                upstream = (getattr(row, "evidence_json", None) or {}).get(
                    "upstream_error_message"
                )
                note_summary = failure.message
                if isinstance(upstream, str) and upstream.strip():
                    note_summary = f"{failure.message} 上游提示：{upstream.strip()[:200]}"
                trajectory.results.append(
                    EvidenceNote(
                        step_id=pending.id,
                        tool=pending.internal_tool_name,
                        status="failed",
                        summary=note_summary,
                    )
                )
            if not await self.repository.save_trajectory(
                task.id, self.worker_id, trajectory.as_plan_json()
            ):
                return
        has_settled = any(note.status == "settled" for note in trajectory.results)
        has_failures = any(note.status == "failed" for note in trajectory.results)
        if not has_settled:
            await self.repository.mark_failed(task.id, self.worker_id, "mcp_call_failed")
            return
        if self.artifacts is not None:
            build_report = getattr(self.artifacts, "build_analysis_report", None)
            if build_report is not None:
                await build_report(task.id)
        await self.checkpoint("after_bi")
        if self.artifacts is not None:
            stream = getattr(self.artifacts, "stream_analysis_summary", None)
            if stream is not None:
                await stream(task.id)
        if has_failures:
            await self.repository.mark_completed_with_warnings(
                task.id,
                self.worker_id,
                "mcp_partial_failure",
                "部分社媒渠道查询失败，报告已基于可用数据生成。",
            )
        else:
            await self.repository.mark_completed(task.id, self.worker_id)
        await self._finish_followups(task.id)

    async def _finish_followups(self, task_id: str) -> None:
        prepare_followups = getattr(self.artifacts, "prepare_followups", None)
        if prepare_followups is not None:
            try:
                await prepare_followups(task_id)
            except Exception:
                pass
        generate_followups = getattr(self.artifacts, "generate_followups", None)
        if generate_followups is not None:
            try:
                await generate_followups(task_id)
            except Exception:
                # Follow-up generation is non-fatal by design.
                pass

    async def _load_or_create_replan(
        self,
        task: Any,
        plan: ToolPlan,
        rows: tuple[Any, ...],
        *,
        completed_step_ids: tuple[str, ...] = (),
    ) -> ToolPlan | None:
        saved = getattr(task, "replan_json", None)
        if saved is not None:
            return ToolPlan.model_validate(saved)
        replan_for = getattr(self.planner, "replan", None)
        if replan_for is None:
            return None
        remaining_calls = replan_retry_budget(
            rows, max_calls=int(getattr(task, "max_calls", 10))
        )
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
            if getattr(row, "status", None) in _REPLAN_RETRYABLE_STATUSES
        )
        if not failures:
            return None
        plan_step_kinds = {
            step.id: step.evidence_kind
            for step in plan.steps
        }
        completed_evidence_kinds = tuple(
            sorted(
                {
                    kind
                    for step_id in completed_step_ids
                    for kind in (plan_step_kinds.get(str(step_id)),)
                    if kind is not None
                }
            )
        )
        context = await self.context_builder.build(task.user_id, task.session_id)
        supplement = await replan_for(
            context,
            ReplanContext(
                completed_step_ids=tuple(
                    sorted(
                        {
                            *completed_step_ids,
                            *(
                                str(getattr(row, "plan_step_id", ""))
                                for row in rows
                                if getattr(row, "status", None) in {"settled", "succeeded"}
                            ),
                        }
                    )
                ),
                completed_evidence_kinds=completed_evidence_kinds,
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
