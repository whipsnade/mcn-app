from __future__ import annotations

import json
import logging
import os
from collections.abc import Sequence
from datetime import UTC, date, datetime
from functools import lru_cache
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.db.session import SessionFactory
from app.goals.context import GoalPlannerContextBuilder
from app.goals.models import TaskGoal
from app.goals.planner import GoalPlannerService
from app.goals.policies import policy_for
from app.goals.summary import build_goal_result_summary
from app.artifacts.models import TaskArtifact
from app.artifacts.service import ArtifactService, module_key_of
from app.identity.models import User, UserChannelPermission
from app.mcp_gateway.datatap import DataTapTransport
from app.mcp_gateway.contracts import DataTapService
from app.mcp_gateway.registry import ToolRegistryService
from app.mcp_gateway.service import McpGatewayService
from app.model.dependencies import get_model_adapter
from app.model.contracts import ChatMessage, StructuredModelRequest
from app.model.exemplars import find_success_exemplars
from app.model.persona import describe_user_persona
from app.orchestration.context import compress_messages
from app.orchestration.loop import AgentDecision, AgentLoopContext
from app.orchestration.routing import extract_requested_period
from app.orchestration.schemas import PlannerTool
from app.reporting.builders import (
    collect_goal_evidence,
    run_brand_analysis,
    run_campaign_analysis,
)
from app.reporting.models import AnalysisReport
from app.selection.analysis import run_kol_analysis
from app.selection.contract import build_export_field_contract
from app.selection.models import KolSelectionSet
from app.selection.service import KolSelectionService
from app.tasks.executor import TaskExecutor, TaskRunner
from app.tasks.followups import FollowupSuggestionService
from app.tasks.models import AnalysisTask, TaskEvent
from app.tasks.recovery import TaskRecovery
from app.tasks.repository import TaskRepository
from app.thinking.contracts import ThinkingOperationSpec
from app.thinking.persistence import ThinkingMessageStore
from app.thinking.service import (
    SessionThinkingService,
    get_session_thinking_service,
)
from app.workspace.models import Message
from app.tasks.state import TaskEventType
from app.workspace.service import WorkspaceService


logger = logging.getLogger(__name__)

def _goal_evidence(trajectory_json: Any, goal_id: str) -> list[dict]:
    """摘要证据：v2 轨迹按 goal_id 切片提取 settled results，v1 取全量。"""
    if isinstance(trajectory_json, dict) and trajectory_json.get("schema") == "agent_trajectory_v2":
        goal_slice = (trajectory_json.get("goals") or {}).get(goal_id) or {}
        return collect_goal_evidence({"results": goal_slice.get("results") or []})
    return collect_goal_evidence(trajectory_json)


# goal_type → (artifact_type, 报告构建器, report.updated 事件 label, 失败占位标题)
_ANALYSIS_GOAL_TABLE = {
    "brand_analysis": ("brand_report", run_brand_analysis, "品牌分析报告已生成", "品牌分析报告"),
    "campaign_analysis": (
        "campaign_report",
        run_campaign_analysis,
        "活动复盘报告已生成",
        "活动复盘报告",
    ),
}


def agent_loop_tags(context: AgentLoopContext) -> list[str]:
    """agent_loop 日志/案例标签：平台 + 澄清画像行业。"""
    tags = [f"platform:{channel}" for channel in context.allowed_channels]
    category = context.param_profile.get("category")
    if isinstance(category, str) and category.strip():
        tags.append(f"industry:{category.strip()}")
    return tags


def param_profile_period_override(profile: dict[str, Any]) -> dict[str, Any] | None:
    """澄清画像含合法 period（start/end，YYYY-MM-DD）时生成覆写时间窗。"""
    period = profile.get("period")
    if not isinstance(period, dict):
        return None
    try:
        start = date.fromisoformat(str(period.get("start") or ""))
        end = date.fromisoformat(str(period.get("end") or ""))
    except ValueError:
        return None
    if end < start:
        return None
    return {
        "unit": "day",
        "value": (end - start).days,
        "start": start.isoformat(),
        "end": end.isoformat(),
    }


class DatabaseTaskStore:
    """每个状态变更都以短事务提交，网络调用不持有数据库事务。"""

    async def _write(self, method: str, *args: Any):
        async with SessionFactory.begin() as db:
            return await getattr(TaskRepository(db), method)(*args)

    async def _read(self, method: str, *args: Any):
        async with SessionFactory() as db:
            return await getattr(TaskRepository(db), method)(*args)

    async def claim_lease(self, *args: Any): return await self._write("claim_lease", *args)
    async def save_plan(self, *args: Any): return await self._write("save_plan", *args)
    async def save_trajectory(self, *args: Any): return await self._write("save_trajectory", *args)
    async def cancel_requested(self, *args: Any): return await self._read("cancel_requested", *args)
    async def renew_lease(self, *args: Any): return await self._write("renew_lease", *args)
    async def mark_completed(self, *args: Any) -> bool:
        return await self._write("mark_completed", *args)
    async def mark_completed_with_warnings(self, *args: Any) -> bool:
        return await self._write("mark_completed_with_warnings", *args)
    async def mark_cancelled(self, *args: Any) -> bool:
        return await self._write("mark_cancelled", *args)
    async def mark_interrupted(self, *args: Any) -> bool:
        return await self._write("mark_interrupted", *args)
    async def mark_failed(self, *args: Any) -> bool:
        return await self._write("mark_failed", *args)
    async def mark_insufficient_balance(self, *args: Any) -> bool:
        return await self._write("mark_insufficient_balance", *args)
    async def release_lease(self, *args: Any): return await self._write("release_lease", *args)
    async def recoverable_task_ids(self): return await self._read("recoverable_task_ids")
    async def pending_followup_task_ids(self): return await self._read("pending_followup_task_ids")
    async def release_expired_unknown(self, *args: Any):
        return await self._write("release_expired_unknown", *args)
    async def append_event(self, *args: Any): return await self._write("append_event", *args)
    async def load_task_goal(self, *args: Any): return await self._read("get_task_goal", *args)
    async def get_task_goals(self, *args: Any): return await self._read("get_task_goals", *args)
    async def mark_goal_running(self, *args: Any):
        return await self._write("mark_goal_running", *args)


class _Permissions:
    async def list_enabled_channels(self, user_id: str) -> Sequence[str]:
        async with SessionFactory() as db:
            return list(
                (await db.scalars(
                    select(UserChannelPermission.channel).where(
                        UserChannelPermission.user_id == user_id,
                        UserChannelPermission.is_enabled.is_(True),
                    )
                )).all()
            )


class _PlanArguments:
    async def load_arguments(self, *, task_id: str, plan_step_id: str) -> dict:
        async with SessionFactory() as db:
            task = await db.get(AnalysisTask, task_id)
            if task is None or task.plan_json is None:
                raise LookupError("task_plan_not_found")
            if task.plan_json.get("schema") == "agent_trajectory_v2":
                # v2：step id 含 goal 命名空间（g{S}_step_N），全量扫描各切片。
                for goal_slice in (task.plan_json.get("goals") or {}).values():
                    for step in goal_slice.get("steps", []):
                        if step.get("id") == plan_step_id:
                            return step["arguments"]
            else:
                for step in task.plan_json.get("steps", []):
                    if step.get("id") == plan_step_id:
                        return step["arguments"]
        raise LookupError("task_plan_step_not_found")


class _TaskArtifacts:
    """将执行器的短边界映射为独立数据库事务，恢复时可安全重入。"""

    def __init__(self, worker_id: str, model) -> None:
        self._worker_id = worker_id
        self._model = model
        self._followups = FollowupSuggestionService(model)
        # auto_kol_analysis 产出的报告 id（按 task_id 记录），finalize_goal
        # 登记 kol_report Artifact 时优先取它，缺席再回退会话最新报告。
        self._report_ids: dict[str, str] = {}

    async def prepare_followups(self, task_id: str) -> bool:
        return await self._followups.prepare(task_id)

    async def generate_followups(self, task_id: str) -> bool:
        return await self._followups.generate(task_id)

    async def auto_kol_analysis(self, task_id: str) -> None:
        """任务收尾时自动生成会话级 KOL 分析报告（尽力而为，绝不阻塞终态）。

        名单为空或模型不可用时静默跳过；任何异常只记 warning。成功时向任务
        事件流 append report.updated（payload 与任务级 build() 同格式），
        调用方保证它在终态事件之前发出。
        """
        if self._model is None:
            return
        try:
            async with SessionFactory.begin() as db:
                task = await db.get(AnalysisTask, task_id)
                if task is None:
                    return
                # 幂等护栏（与 write_conclusion_message 的 existing-check 同模式）：
                # 崩溃恢复重放收尾段时，事件与报告在同一事务已提交，任务事件流
                # 里存在 report.updated 即说明已生成过，直接跳过。
                already_reported = await db.scalar(
                    select(TaskEvent.id).where(
                        TaskEvent.task_id == task.id,
                        TaskEvent.event_type == TaskEventType.REPORT_UPDATED,
                    )
                )
                if already_reported is not None:
                    logger.debug(
                        "auto_kol_analysis skipped: already reported task_id=%s", task_id
                    )
                    return
                # 空名单护栏与读取路径一致：按最新 selection set 的 item 数判断。
                selection_service = KolSelectionService(db)
                selection_set = await selection_service.latest_selection_set(task.session_id)
                count = (
                    await selection_service.count_items(selection_set.id)
                    if selection_set is not None
                    else 0
                )
                if count == 0:
                    logger.debug(
                        "auto_kol_analysis skipped: empty selection task_id=%s", task_id
                    )
                    return
                thinking_service, turn_id, thinking_sink = await self._thinking_sink(
                    db,
                    task,
                    purpose="kol_analysis",
                    label="正在生成KOL报告",
                )
                try:
                    report = await run_kol_analysis(
                        db,
                        self._model,
                        user_id=task.user_id,
                        session_id=task.session_id,
                        thinking_sink=thinking_sink,
                    )
                finally:
                    await self._persist_thinking(
                        db,
                        thinking_service,
                        task=task,
                        turn_id=turn_id,
                    )
                self._report_ids[task.id] = report.id
                await TaskRepository(db).append_event(
                    task.id,
                    task.user_id,
                    TaskEventType.REPORT_UPDATED,
                    {
                        "report_id": report.id,
                        "version": report.version,
                        "phase": "ai_summary",
                        "label": "KOL 分析报告已生成",
                    },
                )
        except Exception:
            logger.warning("auto_kol_analysis_failed task_id=%s", task_id, exc_info=True)

    async def finalize_goal(
        self,
        task_id: str,
        *,
        goal_id: str,
        terminal_status: str,
        error_code: str | None = None,
        warning_code: str | None = None,
    ) -> None:
        """goal 收尾：set 完成 + Artifact 登记 + goal 终态与事件（尽力而为，绝不阻塞终态）。

        独立短事务；任何异常只记 warning。Artifact 按 artifact_key 幂等 upsert，
        恢复重放不重复建行、不重复发 artifact.updated。成功类终态
        （completed/completed_with_warnings/insufficient_balance）发
        goal.completed，failed/skipped 发 goal.failed。成功收尾时生成结果摘要
        落 goal.result_summary_json（阶段四软依赖注入下游）。
        """
        try:
            async with SessionFactory.begin() as db:
                task = await db.get(AnalysisTask, task_id)
                if task is None:
                    return
                goal = await db.get(TaskGoal, goal_id)
                if goal is None or goal.task_id != task.id:
                    return
                now = datetime.now(UTC).replace(tzinfo=None)
                # 轨迹镜像 task.plan_json（agent_trajectory_v1）。
                goal.trajectory_json = task.plan_json
                artifact_service = ArtifactService(db)
                status_override: str | None = None
                analysis_warning: str | None = None
                if goal.goal_type == "kol_selection":
                    selection_set = await db.scalar(
                        select(KolSelectionSet)
                        .where(KolSelectionSet.goal_id == goal.id)
                        .order_by(KolSelectionSet.version.desc())
                        .limit(1)
                    )
                    if selection_set is not None:
                        item_count = await KolSelectionService(db).count_items(
                            selection_set.id
                        )
                        if item_count > 0:
                            selection_set.status = "completed"
                            selection_set.updated_at = now
                            await self._register_goal_artifact(
                                db,
                                artifact_service,
                                task=task,
                                goal=goal,
                                artifact_key=f"goal:{goal.id}:kol_selection_set",
                                artifact_type="kol_selection_set",
                                title=selection_set.title,
                                version=selection_set.version,
                                selection_set_id=selection_set.id,
                                scope=selection_set.scope_json,
                            )
                    if terminal_status in {
                        "completed",
                        "completed_with_warnings",
                        "insufficient_balance",
                    }:
                        report = await self._goal_report(db, task)
                        if report is not None:
                            await self._register_goal_artifact(
                                db,
                                artifact_service,
                                task=task,
                                goal=goal,
                                artifact_key=f"goal:{goal.id}:kol_report",
                                artifact_type="kol_report",
                                title=report.title,
                                version=report.version,
                                report_id=report.id,
                                scope=report.scope_json,
                            )
                elif goal.goal_type in {"brand_analysis", "campaign_analysis"} and (
                    terminal_status in {"completed", "completed_with_warnings"}
                ):
                    status_override, analysis_warning = await self._finalize_analysis_goal(
                        db, artifact_service, task=task, goal=goal
                    )
                goal.status = status_override or terminal_status
                goal.warning_code = warning_code or analysis_warning
                goal.error_code = error_code
                goal.completed_at = now
                goal.updated_at = now
                if goal.status in {"completed", "completed_with_warnings"}:
                    # 成功收尾：生成结果摘要（模型失败回退代码摘要，绝不阻塞编排）。
                    params = goal.params_json if isinstance(goal.params_json, dict) else None
                    goal.result_summary_json = await build_goal_result_summary(
                        self._model,
                        goal_type=goal.goal_type,
                        scope=params,
                        evidence=_goal_evidence(goal.trajectory_json, goal.id),
                    )
                event_type = (
                    TaskEventType.GOAL_FAILED
                    if goal.status in {"failed", "skipped"}
                    else TaskEventType.GOAL_COMPLETED
                )
                payload: dict[str, Any] = {
                    "goal_id": goal.id,
                    "goal_type": goal.goal_type,
                    "status": goal.status,
                }
                if error_code:
                    payload["error_code"] = error_code
                await TaskRepository(db).append_event(task.id, task.user_id, event_type, payload)
        except Exception:
            logger.warning("finalize_goal_failed task_id=%s", task_id, exc_info=True)

    # goal_type → (artifact_type, 构建器, report.updated 事件 label, 失败占位标题)
    async def _finalize_analysis_goal(
        self, db, artifact_service: ArtifactService, *, task: AnalysisTask, goal: TaskGoal
    ) -> tuple[str | None, str | None]:
        """品牌/活动 goal 收尾：构建报告 → 登记 artifact + 双发事件。

        返回 (status_override, warning_code)：报告生成失败时降级
        completed_with_warnings 并登记 failed artifact（不删证据、任务终态不受影响）。
        """
        artifact_type, builder, label, fallback_title = _ANALYSIS_GOAL_TABLE[goal.goal_type]
        artifact_key = f"goal:{goal.id}:{artifact_type}"
        build_error: Exception | None = None
        report: AnalysisReport | None = None
        if self._model is None:
            build_error = RuntimeError("model_unavailable")
        else:
            label = (
                "正在生成品牌报告"
                if goal.goal_type == "brand_analysis"
                else "正在生成活动报告"
            )
            thinking_service, turn_id, thinking_sink = await self._thinking_sink(
                db,
                task,
                purpose=goal.goal_type,
                label=label,
                goal_id=goal.id,
            )
            try:
                try:
                    report = await builder(
                        db,
                        self._model,
                        user_id=task.user_id,
                        session_id=task.session_id,
                        task=task,
                        goal=goal,
                        thinking_sink=thinking_sink,
                    )
                finally:
                    await self._persist_thinking(
                        db,
                        thinking_service,
                        task=task,
                        turn_id=turn_id,
                    )
            except Exception as error:
                build_error = error
        if build_error is not None:
            code = str(
                getattr(build_error, "code", None) or str(build_error) or "report_build_failed"
            )[:64]
            logger.warning(
                "goal_report_build_failed goal_id=%s error=%s", goal.id, code, exc_info=True
            )
            artifact = await artifact_service.register_artifact(
                user_id=task.user_id,
                session_id=task.session_id,
                artifact_key=artifact_key,
                artifact_type=artifact_type,
                title=fallback_title,
                version=1,
                status="failed",
                task_id=task.id,
                goal_id=goal.id,
                error_code=code,
            )
            await TaskRepository(db).append_event(
                task.id,
                task.user_id,
                TaskEventType.ARTIFACT_UPDATED,
                {
                    "artifact_id": artifact.id,
                    "goal_id": goal.id,
                    "artifact_type": artifact.artifact_type,
                    "module_key": module_key_of(artifact.artifact_type),
                    "version": artifact.version,
                    "title": artifact.title,
                },
            )
            return "completed_with_warnings", code
        assert report is not None
        await self._register_goal_artifact(
            db,
            artifact_service,
            task=task,
            goal=goal,
            artifact_key=artifact_key,
            artifact_type=artifact_type,
            title=report.title,
            version=report.version,
            report_id=report.id,
            scope=report.scope_json,
        )
        await TaskRepository(db).append_event(
            task.id,
            task.user_id,
            TaskEventType.REPORT_UPDATED,
            {
                "report_id": report.id,
                "version": report.version,
                "phase": "ai_summary",
                "label": label,
            },
        )
        return None, None

    async def _goal_report(self, db, task: AnalysisTask) -> AnalysisReport | None:
        """auto_kol_analysis 产出的报告；恢复重放时回退会话最新 kol_analysis 报告。"""
        report_id = self._report_ids.get(task.id)
        if report_id is not None:
            report = await db.get(AnalysisReport, report_id)
            if report is not None:
                return report
        return await db.scalar(
            select(AnalysisReport)
            .where(
                AnalysisReport.session_id == task.session_id,
                AnalysisReport.report_type == "kol_analysis",
                AnalysisReport.status == "completed",
            )
            .order_by(AnalysisReport.version.desc())
            .limit(1)
        )

    async def _register_goal_artifact(
        self,
        db,
        artifact_service: ArtifactService,
        *,
        task: AnalysisTask,
        goal: TaskGoal,
        artifact_key: str,
        artifact_type: str,
        title: str,
        version: int,
        report_id: str | None = None,
        selection_set_id: str | None = None,
        scope: dict[str, Any] | None = None,
    ) -> None:
        existing = await db.scalar(
            select(TaskArtifact).where(TaskArtifact.artifact_key == artifact_key)
        )
        artifact = await artifact_service.register_artifact(
            user_id=task.user_id,
            session_id=task.session_id,
            artifact_key=artifact_key,
            artifact_type=artifact_type,
            title=title,
            version=version,
            status="completed",
            task_id=task.id,
            goal_id=goal.id,
            report_id=report_id,
            selection_set_id=selection_set_id,
            scope=scope,
        )
        # 重放时已存在同版本同状态行：不重复发 artifact.updated。
        if (
            existing is not None
            and existing.version == artifact.version
            and existing.status == artifact.status
        ):
            return
        await TaskRepository(db).append_event(
            task.id,
            task.user_id,
            TaskEventType.ARTIFACT_UPDATED,
            {
                "artifact_id": artifact.id,
                "goal_id": goal.id,
                "artifact_type": artifact.artifact_type,
                "module_key": module_key_of(artifact.artifact_type),
                "version": artifact.version,
                "title": artifact.title,
            },
        )

    async def write_conclusion_message(self, task_id: str, conclusion: str) -> None:
        """任务收尾：把 finish 结论写成一条 assistant 消息（幂等，重试安全）。"""
        async with SessionFactory.begin() as db:
            task = await self._locked_active_task(db, task_id)
            existing = await db.scalar(
                select(Message).where(
                    Message.session_id == task.session_id,
                    Message.user_id == task.user_id,
                    Message.role == "assistant",
                    Message.metadata_json["task_id"].as_string() == task.id,
                    Message.metadata_json["kind"].as_string() == "conclusion",
                )
            )
            if existing is not None:
                await self._attach_turn_to_assistant(db, task, existing)
                return
            text = conclusion.strip()
            if not text:
                count = await KolSelectionService(db).count_selection(
                    session_id=task.session_id
                )
                text = (
                    f"圈选完成，共圈选 {count} 位达人。"
                    "可在右侧「KOL 分析」面板导出 Excel 或点击「分析」生成投放建议。"
                )
            sequence = (
                await db.scalar(
                    select(func.max(Message.sequence)).where(
                        Message.session_id == task.session_id
                    )
                )
                or 0
            ) + 1
            message = Message(
                id=str(uuid4()),
                session_id=task.session_id,
                user_id=task.user_id,
                role="assistant",
                content=text,
                sequence=sequence,
                metadata_json={
                    "task_id": task.id,
                    "kind": "conclusion",
                    "status": "completed",
                },
                created_at=datetime.now(UTC).replace(tzinfo=None),
            )
            db.add(message)
            await db.flush()
            await self._attach_turn_to_assistant(db, task, message)
            await TaskRepository(db).append_event(
                task.id,
                task.user_id,
                TaskEventType.MESSAGE_COMPLETED,
                # 前端 reducer 靠 payload.text 直接渲染气泡（无 message.delta
                # 前置时 draft 为空），结论全文必须随事件下发。
                {"message_id": message.id, "text": text},
            )

    async def _thinking_sink(
        self,
        db,
        task: AnalysisTask,
        *,
        purpose: str,
        label: str,
        goal_id: str | None = None,
    ):
        """从任务触发消息创建 Sink；缺 turn 或内部异常时静默禁用。"""
        try:
            trigger = await db.get(Message, task.trigger_message_id)
            turn_id = (
                (trigger.metadata_json or {}).get("turn_id")
                if trigger is not None
                else None
            )
            if not isinstance(turn_id, str) or not turn_id:
                return None, None, None
            service = get_session_thinking_service()
            sink = service.create_sink(
                ThinkingOperationSpec(
                    operation_id=str(uuid4()),
                    turn_id=turn_id,
                    session_id=task.session_id,
                    user_id=task.user_id,
                    purpose=purpose,
                    label=label,
                    task_id=task.id,
                    goal_id=goal_id,
                )
            )
            return service, turn_id, sink
        except Exception:
            logger.warning(
                "task_thinking_sink_create_failed task_id=%s",
                task.id,
                exc_info=True,
            )
            return None, None, None

    async def _persist_thinking(
        self,
        db,
        service: SessionThinkingService | None,
        *,
        task: AnalysisTask,
        turn_id: str | None,
    ) -> None:
        if service is None or turn_id is None:
            return
        try:
            blocks = await service.completed_blocks(
                turn_id=turn_id,
                user_id=task.user_id,
                session_id=task.session_id,
            )
            store = ThinkingMessageStore(db)
            for block in blocks:
                await store.persist_block(
                    block,
                    user_id=task.user_id,
                    session_id=task.session_id,
                )
        except Exception:
            logger.warning(
                "task_thinking_persist_failed task_id=%s",
                task.id,
                exc_info=True,
            )

    async def _attach_turn_to_assistant(
        self,
        db,
        task: AnalysisTask,
        message: Message,
    ) -> None:
        try:
            trigger = await db.get(Message, task.trigger_message_id)
            turn_id = (
                (trigger.metadata_json or {}).get("turn_id")
                if trigger is not None
                else None
            )
            if not isinstance(turn_id, str) or not turn_id:
                return
            await ThinkingMessageStore(db).attach_turn_to_assistant(
                message,
                user_id=task.user_id,
                session_id=task.session_id,
                turn_id=turn_id,
            )
        except Exception:
            logger.warning(
                "task_conclusion_thinking_attach_failed task_id=%s",
                task.id,
                exc_info=True,
            )

    async def _locked_active_task(self, db, task_id: str) -> AnalysisTask:
        task = await db.scalar(
            select(AnalysisTask).where(AnalysisTask.id == task_id).with_for_update()
        )
        if task is None:
            raise LookupError("task_not_found")
        self._require_active_lease(task)
        return task

    def _require_active_lease(self, task: AnalysisTask) -> None:
        if (
            task.lease_owner != self._worker_id
            or task.lease_expires_at is None
            or task.lease_expires_at <= datetime.now(UTC).replace(tzinfo=None)
        ):
            raise RuntimeError("task_lease_lost")


@lru_cache
def get_mcp_transport():
    settings = get_settings()
    return DataTapTransport(
        token=settings.datatap_mcp_token,
        read_timeout_seconds=settings.datatap_read_timeout_seconds,
    )


class DatabaseSelectionIngest:
    """settled 工具证据 → 圈选名单沉淀；独立短事务，不持有任务循环的连接。"""

    def __init__(self) -> None:
        self._remote_by_internal: dict[str, str] | None = None

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
    ) -> None:
        # 双写过渡：提供了 goal 上下文时先写新表（ensure set + items），
        # 再照旧写旧表 session_kol_selections；两边各自独立事务与异常
        # 兜底，任一失败只记 warning 不影响另一边（阶段五才停写旧表）。
        if goal_id is not None:
            try:
                await self._ingest_to_set(
                    user_id=user_id,
                    session_id=session_id,
                    task_id=task_id,
                    internal_tool_name=internal_tool_name,
                    structured_content=structured_content,
                    arguments=arguments,
                    goal_id=goal_id,
                    set_title=set_title,
                    set_scope=set_scope,
                )
            except Exception:
                logger.warning(
                    "kol_selection_set_ingest_failed task_id=%s", task_id, exc_info=True
                )
        for attempt in (1, 2):
            try:
                async with SessionFactory.begin() as db:
                    mapping = await self._tool_mapping(db)
                    if internal_tool_name not in mapping:
                        return
                    # normalizers 适配器按内部工具名匹配；remote 映射仅作为
                    # “该工具仍为已审核启用”的护栏。
                    await KolSelectionService(db).ingest_tool_evidence(
                        user_id=user_id,
                        session_id=session_id,
                        task_id=task_id,
                        tool_name=internal_tool_name,
                        structured_content=structured_content,
                        arguments=arguments,
                    )
                return
            except IntegrityError:
                # 并发 upsert 撞唯一约束：整批回滚后用新事务重试一次，
                # 第二次 select 会命中已有行走 merge；再失败只记 warning。
                if attempt == 2:
                    logger.warning("kol_selection_ingest_conflict", exc_info=True)

    async def _ingest_to_set(
        self,
        *,
        user_id: str,
        session_id: str,
        task_id: str,
        internal_tool_name: str,
        structured_content: Any,
        arguments: dict | None,
        goal_id: str,
        set_title: str,
        set_scope: dict | None,
    ) -> None:
        async with SessionFactory.begin() as db:
            mapping = await self._tool_mapping(db)
            if internal_tool_name not in mapping:
                return
            service = KolSelectionService(db)
            selection_set = await service.ensure_selection_set(
                user_id,
                session_id,
                task_id=task_id,
                goal_id=goal_id,
                title=set_title,
                scope=set_scope,
            )
            await service.ingest_tool_evidence_to_set(
                user_id=user_id,
                selection_set_id=selection_set.id,
                task_id=task_id,
                tool_name=internal_tool_name,
                structured_content=structured_content,
                arguments=arguments,
            )

    async def _tool_mapping(self, db) -> dict[str, str]:
        if self._remote_by_internal is None:
            tools = await ToolRegistryService(db, get_mcp_transport()).list_enabled()
            self._remote_by_internal = {
                tool.internal_name: tool.remote_name for tool in tools
            }
        return self._remote_by_internal


class TaskExecutionDependencies:
    def __init__(self) -> None:
        settings = get_settings()
        self.store = DatabaseTaskStore()
        self.worker_id_prefix = f"inproc-{os.getpid()}"
        self._model = get_model_adapter()
        self._goal_planner_shadow = (
            GoalPlannerService(
                model=self._model,
                context_builder=GoalPlannerContextBuilder(),
            )
            if settings.goal_planner_shadow_enabled
            else None
        )
        self._followups = FollowupSuggestionService(self._model)
        self._transport = get_mcp_transport()
        self._arguments = _PlanArguments()
        self._selection = DatabaseSelectionIngest()

    async def build_agent_context(
        self,
        user_id: str,
        session_id: str,
        *,
        goal_type: str = "kol_selection",
        goal_params: dict | None = None,
    ) -> AgentLoopContext:
        """迭代循环的轻量上下文：消息 + 已审核工具 + 渠道权限，无会话表单约束。

        goal_params 合并进 param_profile（brand/period/platforms 等键优先于
        brainstorm 画像）；export_contract 仅 kol_selection 注入（GoalPolicy）。
        """
        policy = policy_for(goal_type)
        goal_params = dict(goal_params or {})
        async with SessionFactory() as db:
            workspace_service = WorkspaceService(db)
            workspace = await workspace_service.get_owned_session(user_id, session_id)
            messages = await workspace_service.list_messages(user_id, session_id)
            tools = await ToolRegistryService(db, self._transport).list_enabled()
            user = await db.get(User, user_id)
        approved_channels = set(await _Permissions().list_enabled_channels(user_id))
        selected_channels = tuple(
            platform for platform in workspace.platforms if platform in approved_channels
        )
        effective_channels = selected_channels or tuple(sorted(approved_channels))
        recent_messages = compress_messages(messages, max_chars=24_000)
        param_profile = (workspace.filters_snapshot or {}).get("brainstorm_profile") or {}
        if not isinstance(param_profile, dict):
            param_profile = {}
        # goal_params 优先于 brainstorm 画像（planner 确认的目标参数）。
        merged_profile = {**param_profile, **goal_params}
        requested_period = extract_requested_period(
            "\n".join(message.content for message in recent_messages)
        )
        period_override = param_profile_period_override(merged_profile)
        if period_override is not None:
            requested_period = period_override
        context = AgentLoopContext(
            recent_messages=recent_messages,
            tools=tuple(PlannerTool.from_approved(item) for item in tools),
            allowed_channels=effective_channels,
            current_date=date.today().isoformat(),
            requested_period=requested_period,
            param_profile=merged_profile,
            user_persona=describe_user_persona(
                list(user.industries) if user is not None and user.industries else []
            ),
            export_contract=(
                build_export_field_contract(workspace).model_dump(mode="json")
                if policy.inject_export_contract
                else {}
            ),
            goal_type=goal_type,
            goal_params=goal_params,
        )
        context.log_context = {
            "user_id": user_id,
            "session_id": session_id,
            "tags": agent_loop_tags(context),
        }
        return context

    async def agent_decide(self, context: AgentLoopContext) -> AgentDecision:
        tags = [str(tag) for tag in context.log_context.get("tags") or ()]
        user_id = context.log_context.get("user_id")
        session_id = context.log_context.get("session_id")
        task_id = context.log_context.get("task_id")
        goal_id = context.log_context.get("goal_id")
        turn_id = context.log_context.get("turn_id")
        async with SessionFactory() as db:
            exemplars = await find_success_exemplars(
                db,
                purpose="agent_loop",
                tags=tags,
                user_id=user_id if isinstance(user_id, str) else None,
            )
            if isinstance(task_id, str) and (
                not isinstance(turn_id, str) or not isinstance(goal_id, str)
            ):
                task = await db.get(AnalysisTask, task_id)
                if task is not None:
                    trigger = await db.get(Message, task.trigger_message_id)
                    candidate_turn = (
                        (trigger.metadata_json or {}).get("turn_id")
                        if trigger is not None
                        else None
                    )
                    if isinstance(candidate_turn, str) and candidate_turn:
                        turn_id = candidate_turn
                    if not isinstance(goal_id, str):
                        running_goal = await db.scalar(
                            select(TaskGoal)
                            .where(
                                TaskGoal.task_id == task_id,
                                TaskGoal.status == "running",
                            )
                            .order_by(TaskGoal.sequence)
                            .limit(1)
                        )
                        if running_goal is not None:
                            goal_id = running_goal.id
        payload = context.model_dump(mode="json")
        payload["exemplars"] = exemplars
        # 系统 prompt 按 goal_type 分派（GoalPolicy）；purpose 保持 agent_loop 共享案例池。
        policy = policy_for(context.goal_type)
        model_log_context = {
            **context.log_context,
            "task_id": task_id,
            "goal_id": goal_id if isinstance(goal_id, str) else None,
            "turn_id": turn_id if isinstance(turn_id, str) else None,
        }
        context.log_context = model_log_context
        thinking_service: SessionThinkingService | None = None
        thinking_sink = None
        if (
            isinstance(turn_id, str)
            and turn_id
            and isinstance(user_id, str)
            and isinstance(session_id, str)
        ):
            try:
                thinking_service = get_session_thinking_service()
                thinking_sink = thinking_service.create_sink(
                    ThinkingOperationSpec(
                        operation_id=str(uuid4()),
                        turn_id=turn_id,
                        session_id=session_id,
                        user_id=user_id,
                        purpose="agent_loop",
                        label="正在分析数据",
                        task_id=task_id if isinstance(task_id, str) else None,
                        goal_id=goal_id if isinstance(goal_id, str) else None,
                    )
                )
            except Exception:
                logger.warning(
                    "agent_thinking_sink_create_failed task_id=%s",
                    task_id,
                    exc_info=True,
                )
        try:
            result = await self._model.complete_json(
                StructuredModelRequest(
                    purpose="agent_loop",
                    template_name=policy.prompt.name,
                    messages=(
                        ChatMessage(role="system", content=policy.loop_system_prompt()),
                        ChatMessage(
                            role="user",
                            content=json.dumps(
                                payload,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                        ),
                    ),
                    output_model=AgentDecision,
                    max_tokens=4096,
                    log_context=model_log_context,
                    thinking_sink=thinking_sink,
                )
            )
        finally:
            if thinking_service is not None and isinstance(turn_id, str):
                try:
                    blocks = await thinking_service.completed_blocks(
                        turn_id=turn_id,
                        user_id=user_id,
                        session_id=session_id,
                    )
                    if blocks:
                        async with SessionFactory.begin() as db:
                            store = ThinkingMessageStore(db)
                            for block in blocks:
                                await store.persist_block(
                                    block,
                                    user_id=user_id,
                                    session_id=session_id,
                                )
                except Exception:
                    logger.warning(
                        "agent_thinking_persist_failed task_id=%s",
                        task_id,
                        exc_info=True,
                    )
        return result.value

    async def execute_batch(self, commands):
        async with SessionFactory() as db:
            return await McpGatewayService(
                db,
                self._transport,
                arguments_loader=self._arguments,
                registry=ToolRegistryService(db, self._transport),
            ).execute_batch(commands)

    def create_executor(self) -> TaskExecutor:
        worker_id = f"{self.worker_id_prefix}-{uuid4()}"
        return TaskExecutor(
            repository=self.store,
            context_builder=self,
            planner=self,
            gateway=self,
            artifacts=_TaskArtifacts(worker_id, get_model_adapter()),
            selection=self._selection,
            goal_planner_shadow=self._goal_planner_shadow,
            worker_id=worker_id,
            lease_seconds=get_settings().task_lease_seconds,
        )

    def create_runner(self) -> TaskRunner:
        return TaskRunner(
            self.create_executor,
            followup_preparer=self._followups.prepare,
            followup_generator=self._followups.generate,
        )

    def create_recovery(self, runner: TaskRunner) -> TaskRecovery:
        return TaskRecovery(
            repository=self.store,
            runner=runner,
            observation_seconds=int(get_settings().mcp_unknown_reconcile_seconds),
            followup_generator=self._followups.generate,
            followup_preparer=self._followups.prepare,
        )


def create_task_runtime() -> tuple[TaskRunner, TaskRecovery]:
    dependencies = TaskExecutionDependencies()
    runner = dependencies.create_runner()
    return runner, dependencies.create_recovery(runner)


async def refresh_approved_datatap_tools() -> None:
    """服务启动时将已审核工具的最新签名写入本地目录。

    目录读取不触发 MCP 工具函数调用，也不计费。
    签名发生变化时注册中心会自动隔离工具，避免任务继续使用未复核的参数契约。
    """
    async with SessionFactory.begin() as db:
        registry = ToolRegistryService(db, get_mcp_transport())
        # Brand insight and all-channel KOL capabilities are independently
        # refreshed. A temporary outage in one service must not hide tools
        # already approved for the remaining channels.
        for service in (
            DataTapService.INSIGHT_CUBE,
            DataTapService.SOCIAL_GROW,
            DataTapService.SOCIAL_GROW_CONTENT,
            DataTapService.BILIBILI,
        ):
            try:
                await registry.refresh_service(service)
            except Exception:
                continue
