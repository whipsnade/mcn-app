from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid5

from app.billing.service import InsufficientPointsError
from app.goals.policies import policy_for
from app.mcp_gateway.service import ExecuteMcpCall
from app.model.contracts import ModelAdapterError
from app.orchestration.loop import (
    AgentLoopContext,
    AgentTrajectory,
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

    async def mark_cancelled(self, task_id: str, worker_id: str) -> bool: ...

    async def renew_lease(self, task_id: str, worker_id: str, lease_seconds: int) -> bool: ...

    async def mark_completed(self, task_id: str, worker_id: str) -> bool: ...

    async def mark_completed_with_warnings(
        self, task_id: str, worker_id: str, warning_code: str, warning_message: str | None = None
    ) -> bool: ...

    async def mark_interrupted(self, task_id: str, worker_id: str) -> bool: ...

    async def mark_failed(
        self, task_id: str, worker_id: str, code: str, message: str | None = None
    ) -> bool: ...

    async def mark_insufficient_balance(self, task_id: str, worker_id: str) -> bool: ...

    async def append_event(
        self, task_id: str, user_id: str, event_type: str, payload: dict[str, Any]
    ) -> Any: ...

    async def release_lease(self, task_id: str, worker_id: str) -> None: ...


class ContextBuilder(Protocol):
    async def build_agent_context(
        self,
        user_id: str,
        session_id: str,
        *,
        goal_type: str = "kol_selection",
        goal_params: dict | None = None,
    ) -> AgentLoopContext: ...


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
        goal_id: str | None = None,
        set_title: str = "默认名单",
        set_scope: dict | None = None,
    ) -> None: ...


Checkpoint = Callable[[str], Awaitable[None]]
logger = logging.getLogger(__name__)

# 同一工具累计返回空数据达到上限后禁止再调（继续调只会白烧积分）；
# 连续被熔断达到上限则按现有证据收尾，防止零成本死循环。
_MAX_EMPTY_CALLS_PER_TOOL = 2
_MAX_THROTTLE_STREAK = 3

# 缺工具名决策（AGENT_DECISION_MISSING_TOOL_NAME）的连续修正机会：回喂结构化
# 错因后允许模型自我修正，超过上限仍有证据则按受限交付收尾（生产事故教训：
# 笼统回喂无法修正误嵌 arguments 形态，熔断后已采集证据被整体丢弃）。
_MAX_MISSING_TOOL_NAME_STREAK = 2

# goal_type → 目标提示文案（缺工具名回喂用；params.requirement 优先）。
_GOAL_TYPE_LABELS = {
    "kol_selection": "KOL 圈选与导出",
    "brand_analysis": "品牌分析",
    "campaign_analysis": "活动分析",
}

# 终态 goal（恢复时跳过）：与 dependencies.finalize_goal 的落库值一致。
_TERMINAL_GOAL_STATUSES = frozenset(
    {"completed", "completed_with_warnings", "failed", "skipped", "insufficient_balance"}
)

# goal 终态聚合中的「成功类」集合。
_SUCCESS_GOAL_STATUSES = frozenset({"completed", "completed_with_warnings"})


@dataclass
class GoalOutcome:
    """单个 goal 循环的终局信息（多 Goal 编排按此决定收尾动作）。"""

    finish_conclusion: str = ""
    has_settled: bool = False
    has_failures: bool = False
    balance_exhausted: bool = False
    cancelled: bool = False
    interrupted: bool = False
    lease_write_failed: bool = False
    # 缺工具名决策连续修正耗尽（>2 次仍缺名）：单 goal 路径据此做受限交付。
    recovery_exhausted: bool = False


def _v2_plan_json(slices: dict[str, AgentTrajectory]) -> dict[str, Any]:
    """轨迹 v2 落库形状：{"schema": "agent_trajectory_v2", "goals": {goal_id: 切片}}。"""
    return {
        "schema": "agent_trajectory_v2",
        "goals": {
            goal_id: trajectory.model_dump(
                mode="json", by_alias=True, include={"steps", "results"}
            )
            for goal_id, trajectory in slices.items()
        },
    }


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


def _missing_tool_name_feedback(
    decision: Any, context: AgentLoopContext, goal_params: dict | None
) -> str:
    """缺工具名决策的结构化回喂：错因（区分误嵌形态）+ 允许工具清单 + 目标提示。"""
    if isinstance(decision.arguments, dict) and "internal_tool_name" in decision.arguments:
        cause = (
            "上一次决策缺少顶层 internal_tool_name 字段：工具名误嵌在 arguments 内，"
            "请把工具名放到顶层 internal_tool_name 字段，arguments 只放工具参数。"
        )
    else:
        cause = (
            "上一次决策缺少顶层 internal_tool_name 字段，"
            "请把要调用的工具名放到顶层 internal_tool_name 字段。"
        )
    tools_text = "；".join(
        f"{tool.internal_name}（{tool.description}）" for tool in context.tools
    )
    requirement = ""
    if isinstance(goal_params, dict):
        requirement = str(goal_params.get("requirement") or "").strip()
    goal_label = requirement or _GOAL_TYPE_LABELS.get(context.goal_type, context.goal_type)
    return f"{cause}\n允许的工具：{tools_text}\n当前目标：{goal_label}。"


def build_tool_event_payload(
    internal_tool_name: str,
    *,
    status: str,
    step_index: int,
    step_total: int | None,
    error_code: str | None = None,
    goal_id: str | None = None,
    upstream_message: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "platform": canonical_platform(internal_tool_name),
        "step_index": step_index,
        "step_total": step_total,
    }
    # goal_id 为 None 时省略该键，保持既有事件 payload 字节级兼容（Task 7 接线）。
    if goal_id is not None:
        payload["goal_id"] = goal_id
    if status in {"failed", "unknown"}:
        failure = safe_error(error_code)
        payload.update({"error_code": failure.code, "message": failure.message})
        # 上游错误原文（mcp_calls 落库前已 safe_upstream_text 脱敏）随事件透传，
        # 与白名单 message 并存；缺失/空白时省略该键。
        if upstream_message and upstream_message.strip():
            payload["upstream_message"] = upstream_message
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
        # 多 Goal 编排中当前执行的 goal（通用异常路径按 goal_id 标 failed）。
        self._active_goal: Any | None = None

    async def _load_goal(self, task: Any) -> Any | None:
        """加载任务的 kol_selection 单 Goal；旧任务/未接线存储返回 None（legacy 分支）。"""
        loader = getattr(self.repository, "load_task_goal", None)
        if loader is None:
            return None
        try:
            return await loader(task.id)
        except Exception:
            logger.warning("task_goal_load_failed task_id=%s", task.id, exc_info=True)
            return None

    async def _start_goal(self, task: Any, goal: Any) -> None:
        """goal 标记 running 并发 goal.started；存储不支持 goal 时只发事件。"""
        marker = getattr(self.repository, "mark_goal_running", None)
        if marker is not None:
            try:
                await marker(goal.id)
            except Exception:
                logger.warning("goal_mark_running_failed task_id=%s", task.id, exc_info=True)
        await self.repository.append_event(
            task.id,
            task.user_id,
            TaskEventType.GOAL_STARTED,
            {"goal_id": goal.id, "goal_type": goal.goal_type, "sequence": goal.sequence},
        )

    async def _finalize_goal(
        self,
        task_id: str,
        goal: Any | None,
        terminal_status: str,
        error_code: str | None = None,
        warning_code: str | None = None,
    ) -> None:
        if goal is None:
            return
        finalize = getattr(self.artifacts, "finalize_goal", None)
        if finalize is None:
            return
        try:
            await finalize(
                task_id,
                goal_id=goal.id,
                terminal_status=terminal_status,
                error_code=error_code,
                warning_code=warning_code,
            )
        except Exception:
            logger.warning("goal_finalize_failed task_id=%s", task_id, exc_info=True)

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
            terminal_persisted = await self._run_agent_loop(task)
            if (
                terminal_persisted
                and self.goal_planner_shadow is not None
                and getattr(task, "retry_of_task_id", None) is None
            ):
                try:
                    await self.goal_planner_shadow.plan_task(task.id)
                except Exception:
                    logger.warning(
                        "goal_planner_shadow_failed task_id=%s",
                        task.id,
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
            goal = self._active_goal or await self._load_goal(task)
            await self._finalize_goal(task.id, goal, "failed", error_code=code)
            await self.repository.mark_failed(task.id, self.worker_id, code)
        finally:
            stop_heartbeat.set()
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            await self.repository.release_lease(task.id, self.worker_id)

    async def _run_agent_loop(self, task: Any) -> bool:
        """迭代式工具调用循环：每轮由模型决定下一步，finish 结论写成 assistant 消息。

        循环没有调用次数上限：退出条件只有模型 finish、取消、积分余额不足
        或异常。仅在旧任务终态已持久化时返回 True；租约写入失败早退返回 False。
        多 Goal（≥2）走 v2 顺序编排；单 Goal/无 Goal 走 v1 单循环（行为与现状一致）。
        """
        build_agent_context = getattr(self.context_builder, "build_agent_context", None)
        decide = getattr(self.planner, "agent_decide", None)
        if build_agent_context is None or decide is None:
            raise PlanValidationError("AGENT_RUNTIME_UNAVAILABLE")
        goals_loader = getattr(self.repository, "get_task_goals", None)
        goals: list[Any] = []
        if goals_loader is not None:
            try:
                goals = list(await goals_loader(task.id))
            except Exception:
                logger.warning("task_goals_load_failed task_id=%s", task.id, exc_info=True)
                goals = []
        if len(goals) >= 2:
            return await self._orchestrate_goals(
                task, goals, goals_loader, build_agent_context, decide
            )
        return await self._run_single_goal_loop(task, build_agent_context, decide)

    async def _run_single_goal_loop(self, task: Any, build_agent_context, decide) -> bool:
        """v1 单 Goal 循环（阶段二/三行为；多 Goal 之外的唯一路径）。"""
        # 阶段二单 Goal 包装：加载任务的 goal（旧任务/恢复无 goal 时 goal_id
        # 保持 None，行为与现状完全一致，不发 goal 事件、不写新表）。
        goal = await self._load_goal(task)
        goal_id: str | None = None
        set_title = "默认名单"
        set_scope: dict | None = None
        # GoalPolicy 分派：legacy（无 goal）等价 kol_selection。
        goal_type = "kol_selection"
        goal_params: dict | None = None
        if goal is not None:
            goal_id = goal.id
            goal_type = getattr(goal, "goal_type", None) or "kol_selection"
            raw_params = getattr(goal, "params_json", None)
            if isinstance(raw_params, dict) and raw_params:
                goal_params = raw_params
                set_scope = raw_params
                brand = raw_params.get("brand")
                if isinstance(brand, str) and brand.strip():
                    set_title = f"{brand.strip()}圈选名单"
            await self._start_goal(task, goal)
        policy = policy_for(goal_type)
        if goal is not None:
            context = await build_agent_context(
                task.user_id,
                task.session_id,
                goal_type=goal_type,
                goal_params=goal_params,
            )
        else:
            # legacy 路径：不传 goal 参数，上下文与现状完全一致。
            context = await build_agent_context(task.user_id, task.session_id)
        trajectory = restore_agent_trajectory(getattr(task, "plan_json", None))
        if getattr(task, "plan_json", None) is None:
            # First run: emit plan.ready once so clients leave the planning phase.
            if not await self.repository.save_plan(
                task.id, self.worker_id, trajectory.as_plan_json()
            ):
                return False

        async def persist() -> bool:
            return await self.repository.save_trajectory(
                task.id, self.worker_id, trajectory.as_plan_json()
            )

        self._active_goal = goal
        outcome = await self._run_goal_loop(
            task,
            context=context,
            decide=decide,
            trajectory=trajectory,
            policy=policy,
            goal_id=goal_id,
            set_title=set_title,
            set_scope=set_scope,
            step_prefix="step",
            persist=persist,
        )
        self._active_goal = None
        if outcome.cancelled:
            await self._finalize_goal(task.id, goal, "skipped")
            return await self.repository.mark_cancelled(task.id, self.worker_id)
        if outcome.lease_write_failed:
            return False
        if outcome.interrupted:
            await self.repository.mark_interrupted(task.id, self.worker_id)
            # interrupted 可被恢复任务重新领取，不是允许影子规划的终态。
            return False
        if outcome.balance_exhausted:
            # 余额不足：已采集的 settled 证据仍写结论消息，再进入
            # insufficient_balance 终态；无任何证据则直接收尾。
            if outcome.has_settled and self.artifacts is not None:
                await self.artifacts.write_conclusion_message(task.id, outcome.finish_conclusion)
                await self.artifacts.auto_kol_analysis(task.id)
            await self._finalize_goal(task.id, goal, "insufficient_balance")
            return await self.repository.mark_insufficient_balance(task.id, self.worker_id)
        if outcome.recovery_exhausted and outcome.has_settled:
            # 缺工具名修正耗尽但有 settled 证据：不判失败，走既有 finalize 管线
            # 做受限交付（报告构建在 finalize_goal 内部完成，这里绝不能直接调
            # _finalize_analysis_goal，否则会重复构建报告）；无证据时落入下方
            # 既有 no_evidence_collected 失败分支。
            await self._finalize_goal(
                task.id,
                goal,
                "completed_with_warnings",
                warning_code="brand_trend_data_unavailable",
            )
            terminal_persisted = await self.repository.mark_completed_with_warnings(
                task.id,
                self.worker_id,
                "decision_recovery_exhausted",
                "部分数据未能获取，已基于已采集数据生成报告。",
            )
            if terminal_persisted:
                await self._finish_followups(task.id)
            return terminal_persisted
        if not outcome.has_settled:
            # 门禁拆除后模型首轮即可 finish，此时可能从未发起过 MCP 调用，
            # 错误码只描述事实：没有采集到任何证据。
            await self._finalize_goal(
                task.id, goal, "failed", error_code="no_evidence_collected"
            )
            return await self.repository.mark_failed(
                task.id,
                self.worker_id,
                "no_evidence_collected",
            )
        if self.artifacts is not None:
            await self.artifacts.write_conclusion_message(task.id, outcome.finish_conclusion)
            # 结论消息之后、终态标记之前触发自动 KOL 分析：report.updated
            # 事件先于任务终态事件发出（SSE 流尚未关闭）。
            await self.artifacts.auto_kol_analysis(task.id)
        if outcome.has_failures:
            await self._finalize_goal(task.id, goal, "completed_with_warnings")
            terminal_persisted = await self.repository.mark_completed_with_warnings(
                task.id,
                self.worker_id,
                "mcp_partial_failure",
                "部分社媒渠道查询失败，结论已基于可用数据生成。",
            )
        else:
            await self._finalize_goal(task.id, goal, "completed")
            terminal_persisted = await self.repository.mark_completed(task.id, self.worker_id)
        if terminal_persisted:
            await self._finish_followups(task.id)
        return terminal_persisted

    async def _orchestrate_goals(
        self, task: Any, goals: list[Any], goals_loader, build_agent_context, decide
    ) -> bool:
        """多 Goal 顺序编排（轨迹 v2，绝不并发）：按 sequence 逐个执行，终态跳过。"""
        slices: dict[str, AgentTrajectory] = {}
        plan_json = getattr(task, "plan_json", None)
        if isinstance(plan_json, dict) and plan_json.get("schema") == "agent_trajectory_v2":
            for goal_id, goal_slice in (plan_json.get("goals") or {}).items():
                slices[goal_id] = AgentTrajectory.model_validate(goal_slice)

        async def persist() -> bool:
            return await self.repository.save_trajectory(
                task.id, self.worker_id, _v2_plan_json(slices)
            )

        if getattr(task, "plan_json", None) is None:
            # First run: emit plan.ready once so clients leave the planning phase.
            if not await self.repository.save_plan(
                task.id, self.worker_id, _v2_plan_json(slices)
            ):
                return False
        for goal in goals:
            if goal.status in _TERMINAL_GOAL_STATUSES:
                continue
            params = goal.params_json if isinstance(goal.params_json, dict) else {}
            # 软依赖组装：上游成功注入摘要；上游失败记 dependency_missing（仍执行）；
            # 下游 kol 缺 brand 且上游非成功 → skipped 不执行。
            dependency_summaries: list[dict] = []
            dependency_failed = False
            upstream_id = getattr(goal, "depends_on_goal_id", None)
            if upstream_id:
                upstream = next((item for item in goals if item.id == upstream_id), None)
                upstream_status = getattr(upstream, "status", None)
                if upstream_status in _SUCCESS_GOAL_STATUSES:
                    summary = getattr(upstream, "result_summary_json", None)
                    if isinstance(summary, dict) and summary:
                        dependency_summaries.append(
                            {"goal_type": upstream.goal_type, **summary}
                        )
                else:
                    dependency_failed = True
            if (
                dependency_failed
                and goal.goal_type == "kol_selection"
                and not str(params.get("brand") or "").strip()
            ):
                await self._finalize_goal(
                    task.id, goal, "skipped", error_code="dependency_missing_brand"
                )
                goal.status = "skipped"
                continue
            set_title = "默认名单"
            set_scope: dict | None = None
            if params:
                set_scope = params
                brand = params.get("brand")
                if isinstance(brand, str) and brand.strip():
                    set_title = f"{brand.strip()}圈选名单"
            await self._start_goal(task, goal)
            policy = policy_for(getattr(goal, "goal_type", None) or "kol_selection")
            context = await build_agent_context(
                task.user_id,
                task.session_id,
                goal_type=goal.goal_type,
                goal_params=params or None,
            )
            if dependency_summaries:
                context = context.model_copy(
                    update={"dependency_summaries": tuple(dependency_summaries)}
                )
            trajectory = slices.setdefault(goal.id, AgentTrajectory())
            self._active_goal = goal
            outcome = await self._run_goal_loop(
                task,
                context=context,
                decide=decide,
                trajectory=trajectory,
                policy=policy,
                goal_id=goal.id,
                set_title=set_title,
                set_scope=set_scope,
                step_prefix=f"g{goal.sequence}_step",
                persist=persist,
            )
            self._active_goal = None
            if outcome.cancelled:
                await self._finalize_goal(task.id, goal, "skipped")
                goal.status = "skipped"
                # 取消：当前 goal 与全部 pending goal 标 skipped。
                for pending_goal in goals:
                    if pending_goal.status == "pending":
                        await self._finalize_goal(task.id, pending_goal, "skipped")
                        pending_goal.status = "skipped"
                return await self.repository.mark_cancelled(task.id, self.worker_id)
            if outcome.lease_write_failed:
                return False
            if outcome.interrupted:
                await self.repository.mark_interrupted(task.id, self.worker_id)
                # interrupted 可被恢复任务重新领取，不是允许影子规划的终态。
                return False
            if outcome.balance_exhausted:
                # 余额不足：已采集的 settled 证据仍写结论消息；后续 goal 保持 pending。
                if outcome.has_settled and self.artifacts is not None:
                    await self.artifacts.write_conclusion_message(
                        task.id, outcome.finish_conclusion
                    )
                    if policy.ingest_enabled:
                        await self.artifacts.auto_kol_analysis(task.id)
                await self._finalize_goal(task.id, goal, "insufficient_balance")
                goal.status = "insufficient_balance"
                return await self.repository.mark_insufficient_balance(
                    task.id, self.worker_id
                )
            if not outcome.has_settled:
                # 门禁拆除后模型首轮即可 finish，此时可能从未发起过 MCP 调用，
                # 错误码只描述事实：没有采集到任何证据。
                await self._finalize_goal(
                    task.id, goal, "failed", error_code="no_evidence_collected"
                )
                goal.status = "failed"
                continue
            if self.artifacts is not None:
                await self.artifacts.write_conclusion_message(
                    task.id, outcome.finish_conclusion
                )
                if policy.ingest_enabled:
                    await self.artifacts.auto_kol_analysis(task.id)
            # 缺工具名修正耗尽（recovery_exhausted）的受限交付本期只在单 goal
            # 路径接线；编排路径按现状处理：耗尽本身不产生 failed 证据，
            # 仍按 has_failures 决定 completed / completed_with_warnings。
            terminal = "completed_with_warnings" if outcome.has_failures else "completed"
            await self._finalize_goal(
                task.id,
                goal,
                terminal,
                warning_code="dependency_missing" if dependency_failed else None,
            )
            goal.status = terminal
            # 刷新状态与结果摘要（finalize 在独立事务落库），供下游软依赖注入。
            try:
                fresh_goals = await goals_loader(task.id)
                fresh_by_id = {item.id: item for item in fresh_goals}
                for current in goals:
                    newer = fresh_by_id.get(current.id)
                    if newer is not None:
                        current.status = newer.status
                        current.result_summary_json = newer.result_summary_json
            except Exception:
                logger.warning(
                    "task_goals_refresh_failed task_id=%s", task.id, exc_info=True
                )
        # 任务终态聚合：全 completed → completed；全 failed → failed；其余 → warnings。
        statuses = [goal.status for goal in goals]
        if all(status == "completed" for status in statuses):
            terminal_persisted = await self.repository.mark_completed(task.id, self.worker_id)
        elif all(status == "failed" for status in statuses):
            terminal_persisted = await self.repository.mark_failed(
                task.id, self.worker_id, "all_goals_failed"
            )
        else:
            terminal_persisted = await self.repository.mark_completed_with_warnings(
                task.id,
                self.worker_id,
                "goals_partial_failure",
                "部分分析目标未达成，结论已基于可用数据生成。",
            )
        if terminal_persisted:
            await self._finish_followups(task.id)
        return terminal_persisted

    async def _run_goal_loop(
        self,
        task: Any,
        *,
        context: AgentLoopContext,
        decide,
        trajectory: AgentTrajectory,
        policy,
        goal_id: str | None,
        set_title: str,
        set_scope: dict | None,
        step_prefix: str,
        persist,
    ) -> GoalOutcome:
        """单 goal 的迭代轮次循环（v1/v2 共用；轨迹与 step 命名由调用方参数化）。"""
        outcome = GoalOutcome()
        feedback: list[EvidenceNote] = []
        invalid_streak = 0
        missing_tool_name_streak = 0
        throttle_streak = 0
        while True:
            if await self.repository.cancel_requested(task.id):
                outcome.cancelled = True
                return outcome
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
                    outcome.finish_conclusion = decision.conclusion
                    break
                # 工具/渠道校验、参数归一化（平台别名、默认三个月时间窗回填、
                # 时间窗钳制、keyword 必填 name）与 Schema 校验一次完成；
                # 持久化与实发都使用归一化后的参数。
                try:
                    _tool, normalized_arguments = resolve_agent_call(decision, context)
                except PlanValidationError as error:
                    if error.code == "AGENT_DECISION_MISSING_TOOL_NAME":
                        # 缺工具名单独计数（不占 invalid_streak）：回喂结构化错因
                        # 给模型修正机会，不调 MCP、不扣积分；连续修正耗尽则跳出
                        # 循环，由终态分支按已采集证据做受限交付。
                        missing_tool_name_streak += 1
                        if missing_tool_name_streak > _MAX_MISSING_TOOL_NAME_STREAK:
                            outcome.recovery_exhausted = True
                            break
                        feedback.append(
                            EvidenceNote(
                                step_id=f"missing_name_{missing_tool_name_streak}",
                                tool="unknown",
                                status="failed",
                                summary=_missing_tool_name_feedback(
                                    decision, context, set_scope
                                ),
                            )
                        )
                        continue
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
                missing_tool_name_streak = 0
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
                    id=f"{step_prefix}_{len(trajectory.results) + 1}",
                    internal_tool_name=decision.internal_tool_name or "",
                    arguments=normalized_arguments,
                    evidence_goal=decision.evidence_goal,
                )
                trajectory.steps.append(pending)
                # Persist BEFORE invoking so the gateway's arguments_loader can
                # reload byte-identical arguments after a crash.
                if not await persist():
                    outcome.lease_write_failed = True
                    return outcome
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
                    goal_id=goal_id,
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
                goal_id=goal_id,
            )
            await self.checkpoint("after_reserve")
            try:
                rows = await self.gateway.execute_batch((command,))
            except InsufficientPointsError:
                # 余额不足以再发起一次调用（预留阶段抛出，未产生计费）：
                # 停止循环，按余额不足收尾。
                outcome.balance_exhausted = True
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
            # 上游错误原文在 mcp_calls 落库前已 safe_upstream_text 脱敏，可随事件透传。
            upstream = None
            if row is not None:
                candidate = (getattr(row, "evidence_json", None) or {}).get(
                    "upstream_error_message"
                )
                if isinstance(candidate, str):
                    upstream = candidate
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
                    goal_id=goal_id,
                    upstream_message=upstream,
                ),
            )
            await self.checkpoint("after_mcp_result")
            if row_status in {"unknown", "planned", "reserved", "running", "succeeded"}:
                # Possibly-sent calls are never replayed in this run; recovery
                # reconciles them later.
                outcome.interrupted = True
                return outcome
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
                if self.selection is not None and policy.ingest_enabled:
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
                            goal_id=goal_id,
                            set_title=set_title,
                            set_scope=set_scope,
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
            if not await persist():
                outcome.lease_write_failed = True
                return outcome
        outcome.has_settled = any(note.status == "settled" for note in trajectory.results)
        outcome.has_failures = any(note.status == "failed" for note in trajectory.results)
        return outcome

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
