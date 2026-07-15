from __future__ import annotations

import os
from collections.abc import Sequence
from functools import lru_cache
from typing import Any

from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import SessionFactory
from app.identity.models import UserChannelPermission
from app.mcp_gateway.datatap import DataTapTransport
from app.mcp_gateway.fake import FakeMcpTransport
from app.mcp_gateway.registry import ToolRegistryService
from app.mcp_gateway.service import McpGatewayService
from app.model.dependencies import get_model_adapter
from app.orchestration.context import ContextBuilder
from app.orchestration.planner import Planner
from app.tasks.executor import TaskExecutor, TaskRunner
from app.tasks.models import AnalysisTask
from app.tasks.recovery import TaskRecovery
from app.tasks.repository import TaskRepository
from app.workspace.service import WorkspaceService


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
    async def cancel_requested(self, *args: Any): return await self._read("cancel_requested", *args)
    async def renew_lease(self, *args: Any): return await self._write("renew_lease", *args)
    async def mark_completed(self, *args: Any): return await self._write("mark_completed", *args)
    async def mark_cancelled(self, *args: Any): return await self._write("mark_cancelled", *args)
    async def mark_interrupted(self, *args: Any): return await self._write("mark_interrupted", *args)
    async def mark_failed(self, *args: Any): return await self._write("mark_failed", *args)
    async def release_lease(self, *args: Any): return await self._write("release_lease", *args)
    async def recoverable_task_ids(self): return await self._read("recoverable_task_ids")
    async def release_expired_unknown(self, *args: Any):
        return await self._write("release_expired_unknown", *args)


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
        raise LookupError("task_plan_step_not_found")


@lru_cache
def get_mcp_transport():
    settings = get_settings()
    if settings.mcp_provider == "fake":
        return FakeMcpTransport()
    token = settings.datatap_mcp_token
    if token is None:
        raise RuntimeError("DATATAP_MCP_TOKEN is required")
    return DataTapTransport(token=token)


class TaskExecutionDependencies:
    def __init__(self) -> None:
        self.store = DatabaseTaskStore()
        self.worker_id = f"inproc-{os.getpid()}"
        self._planner = Planner(model=get_model_adapter())
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
        return TaskExecutor(
            repository=self.store,
            context_builder=self,
            planner=self,
            gateway=self,
            worker_id=self.worker_id,
            lease_seconds=get_settings().task_lease_seconds,
        )

    def create_runner(self) -> TaskRunner:
        return TaskRunner(self.create_executor)

    def create_recovery(self) -> TaskRecovery:
        return TaskRecovery(
            repository=self.store,
            executor_factory=self.create_executor,
            observation_seconds=int(get_settings().mcp_unknown_reconcile_seconds),
        )


def create_task_runtime() -> tuple[TaskRunner, TaskRecovery]:
    dependencies = TaskExecutionDependencies()
    return dependencies.create_runner(), dependencies.create_recovery()
