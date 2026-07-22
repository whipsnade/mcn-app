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
from app.identity.models import User, UserChannelPermission
from app.mcp_gateway.datatap import DataTapTransport
from app.mcp_gateway.contracts import DataTapService
from app.mcp_gateway.registry import ToolRegistryService
from app.mcp_gateway.service import McpGatewayService
from app.model.dependencies import get_model_adapter
from app.model.contracts import ChatMessage, StructuredModelRequest
from app.model.exemplars import find_success_exemplars
from app.model.persona import describe_user_persona
from app.model.prompts import AGENT_LOOP_PROMPT
from app.orchestration.context import compress_messages
from app.orchestration.loop import AgentDecision, AgentLoopContext
from app.orchestration.routing import extract_requested_period
from app.orchestration.schemas import PlannerTool
from app.selection.service import KolSelectionService
from app.tasks.executor import TaskExecutor, TaskRunner
from app.tasks.followups import FollowupSuggestionService
from app.tasks.models import AnalysisTask
from app.tasks.recovery import TaskRecovery
from app.tasks.repository import TaskRepository
from app.workspace.models import Message
from app.tasks.state import TaskEventType
from app.workspace.service import WorkspaceService


logger = logging.getLogger(__name__)


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
    async def mark_completed(self, *args: Any): return await self._write("mark_completed", *args)
    async def mark_completed_with_warnings(self, *args: Any):
        return await self._write("mark_completed_with_warnings", *args)
    async def mark_cancelled(self, *args: Any): return await self._write("mark_cancelled", *args)
    async def mark_interrupted(self, *args: Any): return await self._write("mark_interrupted", *args)
    async def mark_failed(self, *args: Any): return await self._write("mark_failed", *args)
    async def mark_insufficient_balance(self, *args: Any):
        return await self._write("mark_insufficient_balance", *args)
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


class _PlanArguments:
    async def load_arguments(self, *, task_id: str, plan_step_id: str) -> dict:
        async with SessionFactory() as db:
            task = await db.get(AnalysisTask, task_id)
            if task is None or task.plan_json is None:
                raise LookupError("task_plan_not_found")
            for step in task.plan_json.get("steps", []):
                if step.get("id") == plan_step_id:
                    return step["arguments"]
        raise LookupError("task_plan_step_not_found")


class _TaskArtifacts:
    """将执行器的短边界映射为独立数据库事务，恢复时可安全重入。"""

    def __init__(self, worker_id: str, model) -> None:
        self._worker_id = worker_id
        self._followups = FollowupSuggestionService(model)

    async def prepare_followups(self, task_id: str) -> bool:
        return await self._followups.prepare(task_id)

    async def generate_followups(self, task_id: str) -> bool:
        return await self._followups.generate(task_id)

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
            await TaskRepository(db).append_event(
                task.id,
                task.user_id,
                TaskEventType.MESSAGE_COMPLETED,
                {"message_id": message.id},
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
    ) -> None:
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
                    )
                return
            except IntegrityError:
                # 并发 upsert 撞唯一约束：整批回滚后用新事务重试一次，
                # 第二次 select 会命中已有行走 merge；再失败只记 warning。
                if attempt == 2:
                    logger.warning("kol_selection_ingest_conflict", exc_info=True)

    async def _tool_mapping(self, db) -> dict[str, str]:
        if self._remote_by_internal is None:
            tools = await ToolRegistryService(db, get_mcp_transport()).list_enabled()
            self._remote_by_internal = {
                tool.internal_name: tool.remote_name for tool in tools
            }
        return self._remote_by_internal


class TaskExecutionDependencies:
    def __init__(self) -> None:
        self.store = DatabaseTaskStore()
        self.worker_id_prefix = f"inproc-{os.getpid()}"
        self._model = get_model_adapter()
        self._followups = FollowupSuggestionService(self._model)
        self._transport = get_mcp_transport()
        self._arguments = _PlanArguments()
        self._selection = DatabaseSelectionIngest()

    async def build_agent_context(self, user_id: str, session_id: str) -> AgentLoopContext:
        """迭代循环的轻量上下文：消息 + 已审核工具 + 渠道权限，无会话表单约束。"""
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
        requested_period = extract_requested_period(
            "\n".join(message.content for message in recent_messages)
        )
        period_override = param_profile_period_override(param_profile)
        if period_override is not None:
            requested_period = period_override
        context = AgentLoopContext(
            recent_messages=recent_messages,
            tools=tuple(PlannerTool.from_approved(item) for item in tools),
            allowed_channels=effective_channels,
            current_date=date.today().isoformat(),
            requested_period=requested_period,
            param_profile=param_profile,
            user_persona=describe_user_persona(
                list(user.industries) if user is not None and user.industries else []
            ),
        )
        context.log_context = {
            "user_id": user_id,
            "session_id": session_id,
            "tags": agent_loop_tags(context),
        }
        return context

    async def agent_decide(self, context: AgentLoopContext) -> AgentDecision:
        tags = [str(tag) for tag in context.log_context.get("tags") or ()]
        async with SessionFactory() as db:
            exemplars = await find_success_exemplars(db, purpose="agent_loop", tags=tags)
        payload = context.model_dump(mode="json")
        payload["exemplars"] = exemplars
        result = await self._model.complete_json(
            StructuredModelRequest(
                purpose="agent_loop",
                template_name=AGENT_LOOP_PROMPT.name,
                messages=(
                    ChatMessage(role="system", content=AGENT_LOOP_PROMPT.system),
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
                log_context=context.log_context or None,
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

    def create_executor(self) -> TaskExecutor:
        worker_id = f"{self.worker_id_prefix}-{uuid4()}"
        return TaskExecutor(
            repository=self.store,
            context_builder=self,
            planner=self,
            gateway=self,
            artifacts=_TaskArtifacts(worker_id, get_model_adapter()),
            selection=self._selection,
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
