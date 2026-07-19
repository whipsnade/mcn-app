from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid5

from app.billing.service import InsufficientPointsError
from app.mcp_gateway.service import ExecuteMcpCall
from app.model.contracts import ModelAdapterError
from app.orchestration.bi_requirements import MetricDef, metric_coverage, missing_metrics
from app.orchestration.loop import (
    AgentLoopContext,
    EvidenceNote,
    TrajectoryStep,
    resolve_agent_call,
    restore_agent_trajectory,
)
from app.orchestration.schemas import PlanValidationError
from app.reporting.analysis_reports import sanitize_evidence
from app.tasks.errors import canonical_platform, safe_error
from app.tasks.state import TaskEventType


class TaskStore(Protocol):
    async def claim_lease(self, task_id: str, worker_id: str, lease_seconds: int) -> Any: ...

    async def save_plan(self, task_id: str, worker_id: str, plan_json: dict[str, Any]) -> bool: ...

    async def save_trajectory(
        self, task_id: str, worker_id: str, trajectory_json: dict[str, Any]
    ) -> bool: ...

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

    async def mark_insufficient_balance(self, task_id: str, worker_id: str) -> None: ...

    async def append_event(
        self, task_id: str, user_id: str, event_type: str, payload: dict[str, Any]
    ) -> Any: ...

    async def release_lease(self, task_id: str, worker_id: str) -> None: ...


class ContextBuilder(Protocol):
    async def build_agent_context(self, user_id: str, session_id: str) -> AgentLoopContext: ...


class TaskPlanner(Protocol):
    async def agent_decide(self, context: AgentLoopContext) -> Any: ...


class McpBatchGateway(Protocol):
    async def execute_batch(self, commands: tuple[ExecuteMcpCall, ...]) -> tuple[Any, ...]: ...


class TaskArtifacts(Protocol):
    async def build_analysis_report(self, task_id: str) -> Any: ...

    async def stream_analysis_summary(self, task_id: str) -> Any: ...


Checkpoint = Callable[[str], Awaitable[None]]
logger = logging.getLogger(__name__)

# finish 覆盖门禁连续拒绝上限：达到后放行，避免模型无法补齐时死循环。
_MAX_FINISH_REJECT_STREAK = 3


async def _noop_checkpoint(_: str) -> None:
    return None


def build_tool_event_payload(
    internal_tool_name: str,
    *,
    status: str,
    step_index: int,
    step_total: int | None,
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
            # 所有任务统一走 agent 迭代循环（历史 kind="pipeline" 行的固定
            # DAG 路径已移除，恢复时按空轨迹重新进入迭代循环）。
            await self._run_agent_loop(task)
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

    async def _run_agent_loop(self, task: Any) -> None:
        """迭代式工具调用循环：每轮由模型决定下一步，产出版本化自由报告。

        循环没有调用次数上限：退出条件只有模型 finish（且通过必需数据项
        覆盖门禁）、取消、积分余额不足或异常。
        """
        build_agent_context = getattr(self.context_builder, "build_agent_context", None)
        decide = getattr(self.planner, "agent_decide", None)
        if build_agent_context is None or decide is None:
            raise PlanValidationError("AGENT_RUNTIME_UNAVAILABLE")
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
        finish_reject_streak = 0
        balance_exhausted = False
        # finish 门禁以注入上下文的必需数据项清单为准（生产环境由
        # build_agent_context 填入全局 BI_REQUIRED_METRICS）。
        required_metrics = tuple(
            MetricDef(
                key=str(item.get("key", "")),
                label=str(item.get("label", item.get("key", ""))),
                description=str(item.get("description", "")),
                source_tools=tuple(str(tool) for tool in item.get("source_tools", ())),
            )
            for item in context.required_metrics
            if isinstance(item, dict) and item.get("key")
        )
        while True:
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
                    update={"notes": (*trajectory.results, *feedback)}
                )
                decision = await decide(round_context)
                if decision.action == "finish":
                    # 必需数据项覆盖门禁：未覆盖的项回喂给模型补齐（不占
                    # invalid_streak）；连续被拒达到上限后放行，防止模型
                    # 始终无法补齐时死循环。
                    missing = missing_metrics(
                        metric_coverage(trajectory, required_metrics), required_metrics
                    )
                    if missing and finish_reject_streak < _MAX_FINISH_REJECT_STREAK:
                        finish_reject_streak += 1
                        missing_text = "、".join(
                            f"{metric.label}（可尝试：{' / '.join(metric.source_tools[:2])}）"
                            for metric in missing
                        )
                        feedback.append(
                            EvidenceNote(
                                step_id=f"finish_reject_{finish_reject_streak}",
                                tool="metric_coverage_gate",
                                status="failed",
                                summary=(
                                    f"报告必需数据项尚未覆盖，暂不能结束：{missing_text}。"
                                    "请调用对应工具补齐后再 finish；"
                                    "工具调用成功但返回空数据视为该项已满足。"
                                ),
                            )
                        )
                        continue
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
                    step_total=None,
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
            try:
                rows = await self.gateway.execute_batch((command,))
            except InsufficientPointsError:
                # 余额不足以再发起一次调用（预留阶段抛出，未产生计费）：
                # 停止循环，按余额不足收尾。
                balance_exhausted = True
                break
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
                    step_total=None,
                    error_code=getattr(row, "error_type", None),
                ),
            )
            await self.checkpoint("after_mcp_result")
            if row_status in {"unknown", "planned", "reserved", "running", "succeeded"}:
                # Possibly-sent calls are never replayed in this run; recovery
                # reconciles them later.
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
        if balance_exhausted:
            # 余额不足：已采集的 settled 证据仍产出报告与摘要，再进入
            # insufficient_balance 终态；无任何证据则直接收尾。
            if has_settled and self.artifacts is not None:
                build_report = getattr(self.artifacts, "build_analysis_report", None)
                if build_report is not None:
                    await build_report(task.id)
                await self.checkpoint("after_bi")
                stream = getattr(self.artifacts, "stream_analysis_summary", None)
                if stream is not None:
                    await stream(task.id)
            await self.repository.mark_insufficient_balance(task.id, self.worker_id)
            return
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
        # The task terminal event is durable before suggestion generation
        # starts; suggestion failures can therefore never roll it back.
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
            self._tasks.discard(running)
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
