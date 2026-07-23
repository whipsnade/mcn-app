from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid5

from app.billing.service import InsufficientPointsError
from app.mcp_gateway.service import ExecuteMcpCall
from app.model.contracts import ModelAdapterError
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


class GoalPlannerShadow(Protocol):
    async def plan_task(self, task_id: str) -> Any:
        raise NotImplementedError


class McpBatchGateway(Protocol):
    async def execute_batch(self, commands: tuple[ExecuteMcpCall, ...]) -> tuple[Any, ...]: ...


class TaskArtifacts(Protocol):
    async def write_conclusion_message(self, task_id: str, conclusion: str) -> Any: ...

    async def auto_kol_analysis(self, task_id: str) -> Any: ...

    async def prepare_followups(self, task_id: str) -> bool: ...

    async def generate_followups(self, task_id: str) -> bool: ...


class SelectionIngest(Protocol):
    async def ingest(
        self,
        *,
        user_id: str,
        session_id: str,
        task_id: str,
        internal_tool_name: str,
        structured_content: Any,
        arguments: dict | None = None,
    ) -> None: ...


Checkpoint = Callable[[str], Awaitable[None]]
logger = logging.getLogger(__name__)

# 同一工具累计返回空数据达到上限后禁止再调（继续调只会白烧积分）；
# 连续被熔断达到上限则按现有证据收尾，防止零成本死循环。
_MAX_EMPTY_CALLS_PER_TOOL = 2
_MAX_THROTTLE_STREAK = 3


def _is_empty_summary(summary: Any) -> bool:
    """空值判定：None、空 dict/list，或 JSON 编码后为 null/{}/[] 的字符串。"""
    if summary is None:
        return True
    if isinstance(summary, (dict, list)) and not summary:
        return True
    if isinstance(summary, str):
        text = summary.strip()
        if not text:
            return True
        try:
            decoded = json.loads(text)
        except ValueError:
            return False
        return decoded is None or decoded == {} or decoded == []
    return False


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
        selection: SelectionIngest | None = None,
        goal_planner_shadow: GoalPlannerShadow | None = None,
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
        self.selection = selection
        self.goal_planner_shadow = goal_planner_shadow
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
            if (
                self.goal_planner_shadow is not None
                and getattr(task, "retry_of_task_id", None) is None
            ):
                try:
                    await self.goal_planner_shadow.plan_task(task.id)
                except Exception:
                    logger.warning(
                        "goal_planner_shadow_failed task_id=%s",
                        task.id,
                        exc_info=True,
                    )
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
        """迭代式工具调用循环：每轮由模型决定下一步，finish 结论写成 assistant 消息。

        循环没有调用次数上限：退出条件只有模型 finish、取消、积分余额不足
        或异常。
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
        throttle_streak = 0
        balance_exhausted = False
        finish_conclusion = ""
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
                    update={
                        "notes": (*trajectory.results, *feedback),
                        "log_context": {**context.log_context, "task_id": task.id},
                    }
                )
                decision = await decide(round_context)
                if decision.action == "finish":
                    finish_conclusion = decision.conclusion
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
                empty_calls = sum(
                    1
                    for note in trajectory.results
                    if note.tool == decision.internal_tool_name
                    and note.status == "settled"
                    and _is_empty_summary(note.summary)
                )
                if empty_calls >= _MAX_EMPTY_CALLS_PER_TOOL:
                    # 同一工具累计多次返回空数据：继续调用只会白烧积分，
                    # 拒绝并回喂（不占 invalid_streak）；连续被熔断达到
                    # 上限则按现有证据收尾，防止零成本死循环。
                    throttle_streak += 1
                    feedback.append(
                        EvidenceNote(
                            step_id=f"throttle_{throttle_streak}",
                            tool=decision.internal_tool_name or "unknown",
                            status="failed",
                            summary=(
                                f"工具 {decision.internal_tool_name} 已 {empty_calls} 次"
                                "调用成功但返回空数据，禁止重复调用；"
                                "请改用其他工具继续采集圈选数据，或在数据足够时 finish。"
                            ),
                        )
                    )
                    if throttle_streak >= _MAX_THROTTLE_STREAK:
                        break
                    continue
                throttle_streak = 0
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
                structured_content = (getattr(row, "evidence_json", None) or {}).get(
                    "structured_content"
                )
                trajectory.results.append(
                    EvidenceNote(
                        step_id=pending.id,
                        tool=pending.internal_tool_name,
                        status="settled",
                        summary=sanitize_evidence(structured_content),
                    )
                )
                if self.selection is not None:
                    try:
                        await self.selection.ingest(
                            user_id=task.user_id,
                            session_id=task.session_id,
                            task_id=task.id,
                            internal_tool_name=row.internal_tool_name,
                            structured_content=structured_content,
                            # kol.detail/insight 工具的平台身份藏在调用参数里
                            # （platform / datasource），沉淀时必须一并透传。
                            arguments=command.arguments,
                        )
                    except Exception:
                        # 圈选沉淀失败绝不阻塞任务循环。
                        logger.warning("kol_selection_ingest_failed", exc_info=True)
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
            # 余额不足：已采集的 settled 证据仍写结论消息，再进入
            # insufficient_balance 终态；无任何证据则直接收尾。
            if has_settled and self.artifacts is not None:
                await self.artifacts.write_conclusion_message(task.id, finish_conclusion)
                await self.artifacts.auto_kol_analysis(task.id)
            await self.repository.mark_insufficient_balance(task.id, self.worker_id)
            return
        if not has_settled:
            # 门禁拆除后模型首轮即可 finish，此时可能从未发起过 MCP 调用，
            # 错误码只描述事实：没有采集到任何证据。
            await self.repository.mark_failed(task.id, self.worker_id, "no_evidence_collected")
            return
        if self.artifacts is not None:
            await self.artifacts.write_conclusion_message(task.id, finish_conclusion)
            # 结论消息之后、终态标记之前触发自动 KOL 分析：report.updated
            # 事件先于任务终态事件发出（SSE 流尚未关闭）。
            await self.artifacts.auto_kol_analysis(task.id)
        if has_failures:
            await self.repository.mark_completed_with_warnings(
                task.id,
                self.worker_id,
                "mcp_partial_failure",
                "部分社媒渠道查询失败，结论已基于可用数据生成。",
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
