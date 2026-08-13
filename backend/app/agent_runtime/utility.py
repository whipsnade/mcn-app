"""Utility 后台轻量任务（设计文档 §五 utility_v1 / §8.1 / §十四）。

会话标题、Run 摘要、后续建议等后台轻量任务统一走 ``utility_v1`` Profile：
每次调用创建一个 ``run_kind=internal``、``visibility=internal`` 的子
``agent_runs``（不出现于用户执行卡，不计入父 Run 的 Attempt 决策阈值），
只读有界短上下文（最近有限条消息 + Artifact 紧凑目录），经 Task 6 网关的
``utility`` decision root 输出强类型 Utility JSON，并把结果写入对应目标：

- 会话标题 → ``agent_sessions.title``；
- Run 摘要 → ``memory_entries``（memory_type=``run_summary``）；
- 建议 → 父 Run 完成消息的 ``metadata_json['suggestions']``。

Utility 是 best-effort 后台任务：任何失败只把内部 Run 收口为 failed 并记日志，
绝不改变父 Run 的状态 / outcome。

生产接线（§6.4）由 ``UtilityDispatcher`` 承担：``POST messages`` 首条用户消息
提交后触发 ``schedule_session_title``；executor 在用户主 Run 终态 settle 成功
后触发 ``schedule_run_followups``。每个触发是 fire-and-forget 的 asyncio 任务
（独立 DB Session），与 executor 同一生命周期语义：``start`` 前 schedule 安全
空转（窄装配/测试不启动，绝不泄露真实模型调用），``stop`` 拒绝新触发并等待
在途任务完成。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, RootModel, TypeAdapter, model_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_artifacts.models import AgentArtifact, AgentArtifactVersion
from app.agent_runtime.models import AgentMessage, AgentRun, AgentRunAttempt, AgentSession, MemoryEntry
from app.agent_runtime.profiles import get_profile
from app.agent_runtime.repository import AgentRunRepository, utc_now
from app.licensing.service import LicenseService
from app.model.contracts import ChatMessage
from app.runtime_config.service import RuntimeConfigService
from app.tenancy.service import TenantService

logger = logging.getLogger(__name__)

UTILITY_PROFILE_NAME = "utility_v1"

# Utility 只读短上下文：最近消息窗口上限、单条消息内容上限、目录条目上限。
DEFAULT_RECENT_MESSAGE_WINDOW = 6
DEFAULT_MAX_CONTEXT_CHARS = 8_000
DEFAULT_ARTIFACT_DIRECTORY_CAP = 20
_MESSAGE_CONTENT_CAP = 2_000
_TRUNCATED_MESSAGE_CONTENT_CAP = 128


class UtilityDecision(BaseModel):
    """utility_v1 的强类型输出：一次只完成一个任务，只写对应字段。"""

    model_config = ConfigDict(extra="forbid")

    task: Literal["session_title", "run_summary", "suggestions"]
    title: str | None = None
    summary: str | None = None
    suggestions: list[str] | None = None

    @model_validator(mode="after")
    def _require_task_field(self) -> "UtilityDecision":
        if self.task == "session_title" and not self.title:
            raise ValueError("session_title requires title")
        if self.task == "run_summary" and not self.summary:
            raise ValueError("run_summary requires summary")
        if self.task == "suggestions" and not self.suggestions:
            raise ValueError("suggestions requires suggestions")
        return self


class _UtilityDecisionRoot(RootModel[UtilityDecision]):
    pass


# 作为 ``AgentModelGateway.decide(decision_root=...)`` 的输出 Schema。
UTILITY_DECISION_ROOT: type[RootModel[UtilityDecision]] = _UtilityDecisionRoot
UTILITY_DECISION_ADAPTER: TypeAdapter[UtilityDecision] = TypeAdapter(UtilityDecision)


class UtilityRunner:
    """Utility 任务执行器；每个任务创建一个 internal 子 Run 并写强类型结果。"""

    def __init__(
        self,
        *,
        db: AsyncSession,
        gateway: Any,
        worker_id: str = "utility-worker",
        recent_message_window: int = DEFAULT_RECENT_MESSAGE_WINDOW,
        max_context_chars: int = DEFAULT_MAX_CONTEXT_CHARS,
        artifact_directory_cap: int = DEFAULT_ARTIFACT_DIRECTORY_CAP,
        model: str = "utility-model",
    ) -> None:
        self._db = db
        self._gateway = gateway
        self._worker_id = worker_id
        self._recent_message_window = recent_message_window
        self._max_context_chars = max_context_chars
        self._artifact_directory_cap = artifact_directory_cap
        self._model = model
        self._repo = AgentRunRepository(db)
        self._profile = get_profile(UTILITY_PROFILE_NAME)

    # ------------------------------------------------------------------ #
    # 三个任务入口（best-effort）
    # ------------------------------------------------------------------ #

    async def generate_session_title(
        self, *, session_id: str, user_id: str
    ) -> str | None:
        """生成会话标题并写入 ``agent_sessions.title``。

        重命名保护（§6.4）：只覆盖系统默认标题（``新会话%``）；标题已被用户
        改过（或创建时自定义）时直接返回 ``None``，不浪费模型调用。写入用
        条件 UPDATE 原子复核当前标题仍是默认值，模型调用期间发生的并发
        重命名不会被覆盖。
        """
        session = await self._db.get(AgentSession, session_id)
        if session is None or not session.title.startswith("新会话"):
            return None
        try:
            decision = await self._run_utility(
                task="session_title", session_id=session_id, user_id=user_id, parent_run=None
            )
        except Exception:
            logger.exception("utility session_title failed for session %s", session_id)
            return None
        if decision is None or not decision.title:
            return None
        result = await self._db.execute(
            update(AgentSession)
            .where(AgentSession.id == session_id, AgentSession.title.like("新会话%"))
            .values(title=decision.title, updated_at=utc_now())
        )
        if result.rowcount == 0:
            # 并发重命名竞态：标题已不再是默认值，放弃覆盖。
            return None
        await self._db.flush()
        return decision.title

    async def generate_run_summary(self, *, run: AgentRun) -> str | None:
        """生成 Run 摘要并写入 ``memory_entries``（run_summary）。"""
        try:
            decision = await self._run_utility(
                task="run_summary", session_id=run.session_id, user_id=run.user_id, parent_run=run
            )
        except Exception:
            logger.exception("utility run_summary failed for run %s", run.id)
            return None
        if decision is None or not decision.summary:
            return None
        # 同一 source_run 的旧摘要标记 superseded，保留审计历史。
        previous = await self._db.scalars(
            select(MemoryEntry).where(
                MemoryEntry.source_run_id == run.id,
                MemoryEntry.memory_type == "run_summary",
                MemoryEntry.superseded_at.is_(None),
            )
        )
        now = utc_now()
        for entry in previous:
            entry.superseded_at = now
        self._db.add(
            MemoryEntry(
                id=str(uuid4()),
                session_id=run.session_id,
                source_run_id=run.id,
                memory_type="run_summary",
                content_json={"summary": decision.summary},
                created_at=now,
                superseded_at=None,
            )
        )
        await self._db.flush()
        return decision.summary

    async def generate_suggestions(self, *, run: AgentRun) -> list[str] | None:
        """生成后续建议并写入父 Run 完成消息的 metadata。"""
        try:
            decision = await self._run_utility(
                task="suggestions", session_id=run.session_id, user_id=run.user_id, parent_run=run
            )
        except Exception:
            logger.exception("utility suggestions failed for run %s", run.id)
            return None
        if decision is None or not decision.suggestions:
            return None
        assistant = await self._db.scalar(
            select(AgentMessage)
            .where(AgentMessage.run_id == run.id, AgentMessage.role == "assistant")
            .order_by(AgentMessage.sequence.desc())
            .limit(1)
        )
        if assistant is not None:
            metadata = dict(assistant.metadata_json or {})
            metadata["suggestions"] = list(decision.suggestions)
            assistant.metadata_json = metadata
            await self._db.flush()
        return list(decision.suggestions)

    # ------------------------------------------------------------------ #
    # 内部 Run + 有界上下文
    # ------------------------------------------------------------------ #

    async def _run_utility(
        self,
        *,
        task: str,
        session_id: str,
        user_id: str,
        parent_run: AgentRun | None,
    ) -> UtilityDecision | None:
        """创建 internal 子 Run、读短上下文、调网关、写强类型结果并收口。"""
        session = await self._db.get(AgentSession, session_id)
        if parent_run is None:
            tenant_context = await TenantService(self._db).resolve_user(user_id)
            if (
                session is None
                or session.user_id != user_id
                or session.tenant_id != tenant_context.tenant_id
            ):
                raise PermissionError("session_not_found")
            tenant_id = tenant_context.tenant_id
        else:
            if (
                session is None
                or parent_run.user_id != user_id
                or parent_run.session_id != session_id
                or parent_run.tenant_id is None
                or session.user_id != user_id
                or session.tenant_id != parent_run.tenant_id
            ):
                raise PermissionError("parent_run_not_owned")
            tenant_id = parent_run.tenant_id
        license_decision = await LicenseService(self._db).authorize_run(
            tenant_id, user_id, "utility"
        )
        if not license_decision.allowed:
            raise PermissionError(license_decision.code)
        runtime_service = RuntimeConfigService(self._db)
        if parent_run is None:
            runtime_snapshot = await runtime_service.snapshot_for_new_run(
                tenant_id, profile_name=self._profile.full_name
            )
        else:
            runtime_snapshot = await runtime_service.snapshot_for_child_run(
                parent_run, profile_name=self._profile.full_name
            )
        internal_run = AgentRun(
            id=str(uuid4()),
            session_id=session_id,
            user_id=user_id,
            tenant_id=tenant_id,
            parent_run_id=parent_run.id if parent_run is not None else None,
            run_kind="internal",
            visibility="internal",
            profile_name=self._profile.full_name,
            profile_version=self._profile.version,
            model=parent_run.model if parent_run is not None else self._model,
            runtime_backend=runtime_snapshot.runtime_backend,
            runtime_config_version_id=runtime_snapshot.config_version_id,
            runtime_config_snapshot_json=runtime_snapshot.model_dump(mode="json"),
            queued_at=utc_now(),
            prompt_snapshot_json=None,
            status="queued",
            decision_count=0,
            review_count=0,
            revision_count=0,
        )
        self._db.add(internal_run)
        await self._db.flush()
        attempt = await self._repo.begin_attempt(internal_run.id)

        context = await self._build_context(
            task=task, session_id=session_id, user_id=user_id, parent_run=parent_run
        )
        try:
            decision = await self._gateway.decide(
                run=internal_run,
                attempt_id=attempt.id,
                profile=self._profile,
                messages=[ChatMessage(role="user", content=json.dumps(context, ensure_ascii=False))],
                thinking_sink=None,
                step_sequence=1,
                purpose="utility",
                template_name="utility_v1",
                decision_root=UTILITY_DECISION_ROOT,
            )
        except Exception:
            # 内部子 Run 收口为 failed，绝不向上抛出（best-effort）。
            await self._close_internal_run(internal_run, attempt, failed=True, error_code="utility_failed")
            raise
        parsed = UTILITY_DECISION_ADAPTER.validate_python(decision)
        await self._close_internal_run(internal_run, attempt, failed=False)
        return parsed

    async def _close_internal_run(
        self,
        run: AgentRun,
        attempt: AgentRunAttempt,
        *,
        failed: bool,
        error_code: str | None = None,
    ) -> None:
        now = utc_now()
        run.status = "failed" if failed else "completed"
        run.completed_at = now
        run.error_code = error_code
        run.decision_count = 1
        attempt.outcome = "failed" if failed else "completed"
        attempt.ended_at = now
        attempt.decision_count = 1
        await self._db.flush()

    async def _build_context(
        self,
        *,
        task: str,
        session_id: str,
        user_id: str,
        parent_run: AgentRun | None,
    ) -> dict[str, Any]:
        """有界短上下文：最近有限条消息 + Artifact 紧凑目录（最多最近 N 条）。

        串行化后必须落在 ``max_context_chars`` 预算内：超预算时先截断消息正文
        并压缩目录，仍超则逐字段丢弃（目录 → parent_run → 消息），最终只保留
        最小骨架也保证不超预算。
        """
        recent = await self._recent_messages(session_id)
        directory = await self._artifact_directory(session_id)
        directory = directory[-self._artifact_directory_cap :]
        context: dict[str, Any] = {
            "task": task,
            "session_id": session_id,
            "recent_messages": recent,
            "artifact_directory": directory,
        }
        if parent_run is not None:
            context["parent_run"] = {
                "run_id": parent_run.id,
                "status": parent_run.status,
                "profile_name": parent_run.profile_name,
                "decision_count": parent_run.decision_count,
            }
        text = json.dumps(context, ensure_ascii=False, default=str)
        if len(text) <= self._max_context_chars:
            return context
        # 超预算：截断消息正文，优先丢弃最重的字段，重序列化后必须落在预算内。
        context["truncated"] = True
        context["recent_messages"] = [
            {
                "sequence": m["sequence"],
                "role": m["role"],
                "content": m["content"][:_TRUNCATED_MESSAGE_CONTENT_CAP],
            }
            for m in recent
        ]
        for drop_key in ("artifact_directory", "parent_run", "recent_messages"):
            context.pop(drop_key, None)
            text = json.dumps(context, ensure_ascii=False, default=str)
            if len(text) <= self._max_context_chars:
                return context
        # 理论不可达：最小骨架（task + session_id + truncated）必然在预算内。
        return context

    async def _recent_messages(self, session_id: str) -> list[dict[str, Any]]:
        rows = (
            await self._db.scalars(
                select(AgentMessage)
                .where(AgentMessage.session_id == session_id)
                .order_by(AgentMessage.sequence.desc())
                .limit(max(self._recent_message_window, 1))
            )
        ).all()
        rows = list(reversed(rows))
        return [
            {
                "sequence": message.sequence,
                "role": message.role,
                "content": (message.content or "")[:_MESSAGE_CONTENT_CAP],
            }
            for message in rows
        ]

    async def _artifact_directory(self, session_id: str) -> list[dict[str, Any]]:
        artifacts = (
            await self._db.scalars(
                select(AgentArtifact)
                .where(AgentArtifact.session_id == session_id)
                .order_by(AgentArtifact.activity_sequence.asc(), AgentArtifact.created_at.asc())
            )
        ).all()
        if not artifacts:
            return []
        version_rows = (
            await self._db.execute(
                select(
                    AgentArtifactVersion.artifact_id,
                    AgentArtifactVersion.version,
                    AgentArtifactVersion.data_status,
                ).where(
                    AgentArtifactVersion.artifact_id.in_([artifact.id for artifact in artifacts])
                )
            )
        ).all()
        latest: dict[str, Any] = {}
        for version in version_rows:
            current = latest.get(version.artifact_id)
            if current is None or version.version > current.version:
                latest[version.artifact_id] = version
        return [
            {
                "artifact_id": artifact.id,
                "artifact_key": artifact.artifact_key,
                "module": artifact.module,
                "artifact_type": artifact.artifact_type,
                "version": artifact.latest_version,
                "parent_artifact_id": artifact.parent_artifact_id,
                "status": artifact.status,
                "data_status": latest[artifact.id].data_status if artifact.id in latest else None,
            }
            for artifact in artifacts
        ]


class UtilityDispatcher:
    """Utility 后台任务的 best-effort 触发器（§6.4 生产接线）。

    接线点：``POST /agent/sessions/{id}/messages`` 首条用户消息提交后触发
    ``schedule_session_title``；executor 在用户主 Run（session_analyst_v1）
    终态 settle 成功后触发 ``schedule_run_followups``（run_summary +
    suggestions）。

    每个触发是 fire-and-forget 的 asyncio 任务：独立 DB Session 打开/提交，
    任何失败只记日志，绝不侵入 ``settle_terminal`` 事务边界、不影响父 Run
    状态与事件流。与 executor 同一生命周期语义：``start`` 之前 schedule
    安全空转（窄路由测试不启动 dispatcher，不会泄露真实模型/DB 调用）；
    ``stop`` 拒绝新触发并等待在途任务完成。
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        runner_factory: Callable[[AsyncSession], UtilityRunner],
    ) -> None:
        self._session_factory = session_factory
        self._runner_factory = runner_factory
        self._started = False
        self._tasks: set[asyncio.Task[None]] = set()

    def start(self) -> None:
        """开始接受触发（幂等；生产在 lifespan 启动时调用）。"""
        self._started = True

    async def stop(self) -> None:
        """拒绝新触发并等待在途任务完成（优雅关闭）。"""
        self._started = False
        await self.wait_idle()

    def schedule_session_title(
        self, *, session_id: str, user_id: str
    ) -> asyncio.Task[None] | None:
        """首条用户消息提交后触发标题生成；未启动时安全空转返回 None。"""
        if not self._started:
            return None
        return self._spawn(self._session_title_job(session_id=session_id, user_id=user_id))

    def schedule_run_followups(self, *, run_id: str) -> asyncio.Task[None] | None:
        """用户主 Run 终态后触发 run_summary + suggestions；未启动时安全空转。"""
        if not self._started:
            return None
        return self._spawn(self._run_followups_job(run_id=run_id))

    async def wait_idle(self) -> None:
        """等待当前所有已触发任务完成（测试与优雅关闭用）。"""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def _spawn(self, coro: Any) -> asyncio.Task[None]:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._on_done)
        return task

    def _on_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:  # 防御：job 内部已兜底，理论上不可达
            logger.warning("utility background task failed: %s", error)

    async def _session_title_job(self, *, session_id: str, user_id: str) -> None:
        try:
            async with self._session_factory() as db:
                runner = self._runner_factory(db)
                await runner.generate_session_title(session_id=session_id, user_id=user_id)
                await db.commit()
        except Exception:
            logger.exception("utility session_title job failed for session %s", session_id)

    async def _run_followups_job(self, *, run_id: str) -> None:
        try:
            async with self._session_factory() as db:
                run = await db.get(AgentRun, run_id)
                if run is None:
                    return
                runner = self._runner_factory(db)
                await runner.generate_run_summary(run=run)
                await runner.generate_suggestions(run=run)
                await db.commit()
        except Exception:
            logger.exception("utility run followups job failed for run %s", run_id)


__all__ = [
    "DEFAULT_ARTIFACT_DIRECTORY_CAP",
    "DEFAULT_MAX_CONTEXT_CHARS",
    "DEFAULT_RECENT_MESSAGE_WINDOW",
    "UTILITY_DECISION_ADAPTER",
    "UTILITY_DECISION_ROOT",
    "UTILITY_PROFILE_NAME",
    "UtilityDecision",
    "UtilityDispatcher",
    "UtilityRunner",
]
