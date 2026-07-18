from __future__ import annotations

import json
import os
from collections.abc import Sequence
from datetime import UTC, date, datetime
from functools import lru_cache
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.session import SessionFactory
from app.identity.models import UserChannelPermission
from app.mcp_gateway.datatap import DataTapTransport
from app.mcp_gateway.contracts import DataTapService
from app.mcp_gateway.registry import ToolRegistryService
from app.mcp_gateway.service import McpGatewayService
from app.model.dependencies import get_model_adapter
from app.model.contracts import ChatMessage, StreamingModelRequest, StructuredModelRequest
from app.model.prompts import (
    AGENT_LOOP_PROMPT,
    ANALYST_PROMPT,
    REPORT_WRITER_PROMPT,
    SUMMARY_PROMPT,
)
from app.orchestration.context import ContextBuilder, compress_messages
from app.orchestration.loop import AgentDecision, AgentLoopContext
from app.orchestration.planner import Planner
from app.orchestration.routing import extract_requested_period
from app.orchestration.schemas import PlannerTool
from app.tasks.executor import TaskExecutor, TaskRunner
from app.tasks.followups import FollowupSuggestionService
from app.tasks.models import AnalysisTask
from app.tasks.recovery import TaskRecovery
from app.tasks.repository import TaskRepository
from app.workspace.models import Message
from app.reporting.analysis_reports import AnalysisReportService
from app.reporting.blocks import ReportDocument
from app.reporting.models import AnalysisReport, BiReport
from app.reporting.schemas import AnalystConclusion
from app.reporting.service import ReportingService
from app.tasks.state import TaskEventType
from app.workspace.service import WorkspaceService


class SummaryRecoveryMismatch(RuntimeError):
    """恢复流无法证明与已持久化草稿属于同一输出。"""


def summary_deltas_to_persist(
    persisted_content: str, deltas: tuple[str, ...], *, completed: bool = False
) -> tuple[str, ...] | None:
    """流恢复从头重放时，只返回尚未提交的后缀。"""
    if completed:
        return ()
    generated = ""
    persisted = persisted_content
    result: list[str] = []
    for delta in deltas:
        generated += delta
        if persisted.startswith(generated):
            continue
        if generated.startswith(persisted):
            suffix = generated[len(persisted) :]
            if suffix:
                result.append(suffix)
                persisted = generated
            continue
        # 无法证明新流是旧草稿的同一前缀：保留草稿并拒绝混拼。
        return None
    return tuple(result)


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
    async def save_replan(self, *args: Any): return await self._write("save_replan", *args)
    async def cancel_requested(self, *args: Any): return await self._read("cancel_requested", *args)
    async def renew_lease(self, *args: Any): return await self._write("renew_lease", *args)
    async def mark_completed(self, *args: Any): return await self._write("mark_completed", *args)
    async def mark_completed_with_warnings(self, *args: Any):
        return await self._write("mark_completed_with_warnings", *args)
    async def mark_cancelled(self, *args: Any): return await self._write("mark_cancelled", *args)
    async def mark_interrupted(self, *args: Any): return await self._write("mark_interrupted", *args)
    async def mark_failed(self, *args: Any): return await self._write("mark_failed", *args)
    async def release_lease(self, *args: Any): return await self._write("release_lease", *args)
    async def recoverable_task_ids(self): return await self._read("recoverable_task_ids")
    async def pending_followup_task_ids(self): return await self._read("pending_followup_task_ids")
    async def release_expired_unknown(self, *args: Any):
        return await self._write("release_expired_unknown", *args)
    async def append_event(self, *args: Any): return await self._write("append_event", *args)


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


class _ReportingContext:
    async def context_summary(self, session_id: str) -> dict[str, Any]:
        # Task 9 will replace this with candidate/BI projections.
        return {}


class _PlanArguments:
    async def load_arguments(self, *, task_id: str, plan_step_id: str) -> dict:
        async with SessionFactory() as db:
            task = await db.get(AnalysisTask, task_id)
            if task is None or task.plan_json is None:
                raise LookupError("task_plan_not_found")
            for step in task.plan_json.get("steps", []):
                if step.get("id") == plan_step_id:
                    return step["arguments"]
            for step in (task.replan_json or {}).get("steps", []):
                if step.get("id") == plan_step_id:
                    return step["arguments"]
        raise LookupError("task_plan_step_not_found")


class _TaskArtifacts:
    """将执行器的短边界映射为独立数据库事务，恢复时可安全重入。"""

    def __init__(self, worker_id: str, model) -> None:
        self._worker_id = worker_id
        self._model = model
        self._followups = FollowupSuggestionService(model)

    async def prepare_followups(self, task_id: str) -> bool:
        return await self._followups.prepare(task_id)

    async def generate_followups(self, task_id: str) -> bool:
        return await self._followups.generate(task_id)

    async def _profile(self, task_id: str) -> str:
        async with SessionFactory() as db:
            task = await db.get(AnalysisTask, task_id)
            if task is None:
                raise LookupError("task_not_found")
            message = await db.get(Message, task.trigger_message_id)
            value = (message.metadata_json if message is not None else {}).get("scoring_profile")
            return value if value in {"balanced", "audience_first", "performance_first", "budget_first", "risk_first"} else "balanced"

    async def build_candidates(self, task_id: str):
        profile = await self._profile(task_id)
        async with SessionFactory.begin() as db:
            return await ReportingService(db).build_candidate_version(
                task_id, profile, lease_owner=self._worker_id
            )

    async def build_bi_report(self, task_id: str):
        async with SessionFactory() as db:
            analyst_input = await ReportingService(db).analyst_input(task_id)
        result = await self._model.complete_json(
            StructuredModelRequest(
                purpose="analyst",
                template_name=ANALYST_PROMPT.name,
                messages=(
                    ChatMessage(role="system", content=ANALYST_PROMPT.system),
                    ChatMessage(
                        role="user",
                        content=json.dumps(
                            analyst_input,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    ),
                ),
                output_model=AnalystConclusion,
            )
        )
        async with SessionFactory.begin() as db:
            return await ReportingService(db).build_bi_report(
                task_id, analyst_conclusion=result.value, lease_owner=self._worker_id
            )

    async def build_analysis_report(self, task_id: str):
        """agent 任务产物：report_writer 基于已结算证据撰写版本化自由报告。"""
        async with SessionFactory() as db:
            writer_input = await AnalysisReportService(db).writer_input(task_id)
        result = await self._model.complete_json(
            StructuredModelRequest(
                purpose="report_writer",
                template_name=REPORT_WRITER_PROMPT.name,
                messages=(
                    ChatMessage(role="system", content=REPORT_WRITER_PROMPT.system),
                    ChatMessage(
                        role="user",
                        content=json.dumps(
                            writer_input,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    ),
                ),
                output_model=ReportDocument,
                max_tokens=8192,
            )
        )
        async with SessionFactory.begin() as db:
            return await AnalysisReportService(db).build(
                task_id, document=result.value, lease_owner=self._worker_id
            )

    async def stream_summary(self, task_id: str) -> None:
        async with SessionFactory() as db:
            report = await db.scalar(
                select(BiReport).where(BiReport.task_id == task_id).order_by(BiReport.report_version.desc())
            )
            task = await db.get(AnalysisTask, task_id)
            if report is None or task is None:
                raise LookupError("report_not_found")
            summary_input = {
                "task_id": task.id,
                "report_id": report.id,
                "candidate_version": report.candidate_version,
                "overview": report.chart_data_json.get("overview", {}),
                "conclusion": report.conclusion_text or "",
            }
            existing = await self._existing_summary_message(db, task)
        await self._stream_summary(task_id, summary_input, existing)

    async def stream_analysis_summary(self, task_id: str) -> None:
        """agent 任务的摘要：输入为已持久化的自由报告，而非 BI 报告。"""
        async with SessionFactory() as db:
            report = await db.scalar(
                select(AnalysisReport)
                .where(AnalysisReport.task_id == task_id)
                .order_by(AnalysisReport.version.desc())
            )
            task = await db.get(AnalysisTask, task_id)
            if report is None or task is None:
                raise LookupError("report_not_found")
            summary_input = {
                "task_id": task.id,
                "report_id": report.id,
                "report_version": report.version,
                "title": report.title,
                "conclusion": report.conclusion_text or "",
            }
            existing = await self._existing_summary_message(db, task)
        await self._stream_summary(task_id, summary_input, existing)

    async def _stream_summary(
        self, task_id: str, summary_input: dict, existing: Message | None
    ) -> None:
        """逐段提交草稿与事件；客户端断开后执行器仍会完成该过程。"""
        if existing is not None and existing.metadata_json.get("status") == "completed":
            return
        content = existing.content if existing is not None else ""
        message_id = existing.id if existing is not None else None
        generated_deltas: list[str] = []
        async for event in self._model.stream_text(
            StreamingModelRequest(
                messages=(
                    ChatMessage(role="system", content=SUMMARY_PROMPT.system),
                    ChatMessage(
                        role="user",
                        content=json.dumps(summary_input, ensure_ascii=False, sort_keys=True),
                    ),
                )
            )
        ):
            if event.type != "text.delta" or not event.text:
                continue
            generated_deltas.append(event.text)
            pending = summary_deltas_to_persist(content, tuple(generated_deltas))
            if pending is None:
                raise SummaryRecoveryMismatch("summary_recovery_prefix_mismatch")
            if not pending:
                continue
            delta = pending[-1]
            content += delta
            message_id = await self._save_summary_delta(task_id, message_id, content, delta)
        if message_id is None:
            raise ValueError("summary_stream_empty")
        async with SessionFactory.begin() as db:
            task = await self._locked_active_task(db, task_id)
            message = await db.get(Message, message_id)
            if message is None:
                raise LookupError("summary_message_not_found")
            message.metadata_json = {**message.metadata_json, "status": "completed"}
            await TaskRepository(db).append_event(
                task.id,
                task.user_id,
                TaskEventType.MESSAGE_COMPLETED,
                {"message_id": message.id},
            )

    async def _save_summary_delta(
        self, task_id: str, message_id: str | None, content: str, delta: str
    ) -> str:
        async with SessionFactory.begin() as db:
            task = await self._locked_active_task(db, task_id)
            if message_id is None:
                sequence = (
                    await db.scalar(select(func.max(Message.sequence)).where(Message.session_id == task.session_id))
                    or 0
                ) + 1
                message = Message(
                    id=str(uuid4()),
                    session_id=task.session_id,
                    user_id=task.user_id,
                    role="assistant",
                    content=content,
                    sequence=sequence,
                    metadata_json={"task_id": task.id, "status": "streaming"},
                    created_at=datetime.now(UTC).replace(tzinfo=None),
                )
                db.add(message)
                await db.flush()
            else:
                message = await db.get(Message, message_id)
                if message is None:
                    raise LookupError("summary_message_not_found")
                message.content = content
            await TaskRepository(db).append_event(
                task.id,
                task.user_id,
                TaskEventType.MESSAGE_DELTA,
                {"message_id": message.id, "delta": delta},
            )
            return message.id

    async def _locked_active_task(self, db, task_id: str) -> AnalysisTask:
        task = await db.scalar(
            select(AnalysisTask).where(AnalysisTask.id == task_id).with_for_update()
        )
        if task is None:
            raise LookupError("task_not_found")
        self._require_active_lease(task)
        return task

    async def _existing_summary_message(self, db, task: AnalysisTask) -> Message | None:
        messages = list(
            (
                await db.scalars(
                    select(Message)
                    .where(
                        Message.session_id == task.session_id,
                        Message.user_id == task.user_id,
                        Message.role == "assistant",
                    )
                    .order_by(Message.sequence.desc())
                )
            ).all()
        )
        return next(
            (message for message in messages if message.metadata_json.get("task_id") == task.id), None
        )

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


class TaskExecutionDependencies:
    def __init__(self) -> None:
        self.store = DatabaseTaskStore()
        self.worker_id_prefix = f"inproc-{os.getpid()}"
        self._model = get_model_adapter()
        self._planner = Planner(model=self._model)
        self._followups = FollowupSuggestionService(self._model)
        self._transport = get_mcp_transport()
        self._arguments = _PlanArguments()

    async def build(self, user_id: str, session_id: str):
        async with SessionFactory() as db:
            return await ContextBuilder(
                workspace=WorkspaceService(db),
                registry=ToolRegistryService(db, self._transport),
                permissions=_Permissions(),
                reporting=_ReportingContext(),
            ).build(user_id, session_id)

    async def build_agent_context(self, user_id: str, session_id: str) -> AgentLoopContext:
        """迭代循环的轻量上下文：消息 + 已审核工具 + 渠道权限，无会话表单约束。"""
        async with SessionFactory() as db:
            workspace_service = WorkspaceService(db)
            workspace = await workspace_service.get_owned_session(user_id, session_id)
            messages = await workspace_service.list_messages(user_id, session_id)
            tools = await ToolRegistryService(db, self._transport).list_enabled()
        approved_channels = set(await _Permissions().list_enabled_channels(user_id))
        selected_channels = tuple(
            platform for platform in workspace.platforms if platform in approved_channels
        )
        effective_channels = selected_channels or tuple(sorted(approved_channels))
        recent_messages = compress_messages(messages, max_chars=24_000)
        return AgentLoopContext(
            recent_messages=recent_messages,
            tools=tuple(PlannerTool.from_approved(item) for item in tools),
            allowed_channels=effective_channels,
            current_date=date.today().isoformat(),
            requested_period=extract_requested_period(
                "\n".join(message.content for message in recent_messages)
            ),
        )

    async def agent_decide(self, context: AgentLoopContext) -> AgentDecision:
        result = await self._model.complete_json(
            StructuredModelRequest(
                purpose="agent_loop",
                template_name=AGENT_LOOP_PROMPT.name,
                messages=(
                    ChatMessage(role="system", content=AGENT_LOOP_PROMPT.system),
                    ChatMessage(
                        role="user",
                        content=json.dumps(
                            context.model_dump(mode="json"),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    ),
                ),
                output_model=AgentDecision,
                max_tokens=4096,
            )
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

    async def plan(self, context):
        return await self._planner.plan(context)

    def create_executor(self) -> TaskExecutor:
        worker_id = f"{self.worker_id_prefix}-{uuid4()}"
        return TaskExecutor(
            repository=self.store,
            context_builder=self,
            planner=self,
            gateway=self,
            artifacts=_TaskArtifacts(worker_id, get_model_adapter()),
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
            DataTapService.BILIBILI,
        ):
            try:
                await registry.refresh_service(service)
            except Exception:
                continue
