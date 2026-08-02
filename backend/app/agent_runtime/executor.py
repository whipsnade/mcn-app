"""进程级 Agent Run 执行器（设计文档 §四 / §七 / Task 15）。

``AgentRunExecutor`` 是 TaskRunner 等价的新执行器：一个后台 worker 循环按固定
间隔扫描可领取的 Run（queued 新任务 + 过期租约的 running Run），领取后开启/重建
``agent_run_attempts`` 并调用 Task 14 ``AgentEngine.run`` 执行到终态。

**过期租约接管**（§七「恢复时从最后一个完整 Step 继续」）：租约过期的 running
Run 可被新 worker 领取。领取后先 ``pause`` 收口旧 Attempt（outcome=paused），再经
``begin_attempt(resumed=True)`` 开启全新 Attempt（决策/时长计数从零），最后重新
领取租约。引擎的 ``_next_step_sequence`` 从 ``agent_steps`` 现存最大 sequence 继续，
因此完整 Step 绝不重放、sequence 不归零。

**优雅关闭**：``stop()`` 置停止信号，循环只在下一次迭代开始前检查，因此当前
in-flight Run 会在事务安全点自然完成；超时则取消循环任务，留下的 running Run 由
恢复循环在租约过期后接管。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.engine import AgentEngine
from app.agent_runtime.models import AgentMessage, AgentRun, AgentRunAttempt
from app.agent_runtime.profiles import get_profile
from app.agent_runtime.repository import AgentRunRepository, utc_now
from app.agent_runtime.state import InvalidRunTransition, RunStatus
from app.model.contracts import ChatMessage

logger = logging.getLogger(__name__)


class AgentRunExecutor:
    """进程内执行器：后台循环领取 queued / 过期租约 Run 并驱动引擎执行。

    ``session_factory`` 必须是一个可异步上下文管理的会话工厂
    （``async with session_factory() as db`` 产生 :class:`AsyncSession`）；
    ``engine_factory`` 接收 ``(会话, worker_id)`` 并返回绑定该会话与租约
    worker 的 :class:`AgentEngine`。测试可注入
    ``lambda: <共享 AsyncSession 的上下文管理器>``。
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        engine_factory: Callable[[AsyncSession, str], AgentEngine],
        worker_id: str,
        lease_seconds: int = 300,
        claim_interval_seconds: float = 1.0,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds_must_be_positive")
        self._session_factory = session_factory
        self._engine_factory = engine_factory
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._claim_interval_seconds = claim_interval_seconds
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """启动后台 worker 循环（幂等）。"""
        if self._loop_task is None:
            self._loop_task = asyncio.create_task(self._run_loop())

    async def stop(self, *, timeout_seconds: float = 5) -> None:
        """优雅关闭：停止领取新 Run，等待当前 Run 在事务安全点完成。

        超过 ``timeout_seconds`` 则取消循环任务；未完成的 running Run 由恢复
        循环在租约过期后接管（不会静默卡死）。
        """
        self._stop.set()
        if self._loop_task is not None:
            try:
                await asyncio.wait_for(self._loop_task, timeout=timeout_seconds)
            except TimeoutError:
                self._loop_task.cancel()
                await asyncio.gather(self._loop_task, return_exceptions=True)
            self._loop_task = None

    def submit(self, run_id: str) -> None:
        """把新 Run 提交给执行器（Task 19 API 接线入口）。

        执行器是轮询式 worker：queued Run 落库后后台循环自动领取，因此 submit
        不做按 run_id 分发，只唤醒循环立即扫描以缩短创建→执行的延迟；后台循环
        尚未启动（如窄路由测试 / 单测）或已停止时安全空转。幂等安全。
        """
        del run_id
        if self._loop_task is not None and not self._stop.is_set():
            self._wake.set()

    async def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.claim_and_process_one()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("agent executor loop iteration failed")
            # 等待下一领取间隔；submit() 置位 _wake 时立即唤醒，不等待完整间隔。
            stop_wait = asyncio.ensure_future(self._stop.wait())
            wake_wait = asyncio.ensure_future(self._wake.wait())
            try:
                await asyncio.wait(
                    {stop_wait, wake_wait}, timeout=self._claim_interval_seconds
                )
            finally:
                self._wake.clear()
                stop_wait.cancel()
                wake_wait.cancel()

    # ------------------------------------------------------------------ #
    # 领取 / 执行
    # ------------------------------------------------------------------ #

    async def claim_and_process_one(self) -> str | None:
        """领取一个可执行 Run 并驱动引擎执行到终态；无可领取时返回 None。

        已停止（``stop`` 后）的 executor 不再领取任何新 Run。
        """
        if self._stop.is_set():
            return None
        async with self._session_factory() as db:
            run_id = await self._find_claimable_id(db)
            if run_id is None:
                return None
            await self._process_run(db, run_id, self._worker_id)
        return run_id

    async def process_run(
        self, run_id: str, *, worker_id: str | None = None
    ) -> RunStatus | None:
        """对指定 Run 执行一次完整领取 + 引擎执行（恢复循环入口）。

        ``worker_id`` 默认取 executor 自己的；恢复循环可传入独立 worker id，
        避免与原 worker 共用租约导致同一 Run 被并发执行（Fix 4）。
        """
        async with self._session_factory() as db:
            return await self._process_run(db, run_id, worker_id or self._worker_id)

    async def _process_run(
        self, db: AsyncSession, run_id: str, worker_id: str
    ) -> RunStatus | None:
        repo = AgentRunRepository(db)
        prepared = await self._claim_and_prepare(db, repo, run_id, worker_id)
        if prepared is None:
            return None
        run, attempt = prepared
        try:
            messages = await self._build_messages(db, run)
            engine = self._engine_factory(db, worker_id)
            outcome = await engine.run(
                run=run,
                attempt_id=attempt.id,
                profile=get_profile(run.profile_name),
                messages=messages,
            )
            await db.commit()
            return outcome.status
        except asyncio.CancelledError:
            # 关闭/取消：不强行收口，交给恢复循环在租约过期后接管。
            raise
        except Exception:
            logger.exception("agent run %s failed in executor", run_id)
            await self._finalize_failed(db, repo, run_id, worker_id)
            await db.commit()
            return RunStatus.FAILED

    async def _find_claimable_id(self, db: AsyncSession) -> str | None:
        """返回一个可领取的 user Run id：queued 新任务优先，其次过期/无租约 RUNNING。

        无租约 RUNNING（``lease_expires_at`` 为 NULL）是 API resume 经
        ``begin_attempt(resumed=True)`` 或 begin_attempt 后崩溃留下的状态：
        当前 Attempt 已就绪、无活跃 worker 持有，必须可被领取，否则恢复后
        的 Run 会永久卡在 running。
        """
        now = utc_now()
        queued = await db.scalar(
            select(AgentRun.id)
            .where(
                AgentRun.run_kind == "user",
                AgentRun.status == RunStatus.QUEUED,
                AgentRun.cancel_requested.is_(False),
            )
            .order_by(AgentRun.id.asc())
            .limit(1)
        )
        if queued is not None:
            return queued
        return await db.scalar(
            select(AgentRun.id)
            .where(
                AgentRun.run_kind == "user",
                AgentRun.status == RunStatus.RUNNING,
                AgentRun.cancel_requested.is_(False),
                or_(
                    AgentRun.lease_expires_at.is_(None),
                    AgentRun.lease_expires_at <= now,
                ),
            )
            .order_by(AgentRun.id.asc())
            .limit(1)
        )

    async def _claim_and_prepare(
        self, db: AsyncSession, repo: AgentRunRepository, run_id: str, worker_id: str
    ) -> tuple[AgentRun, AgentRunAttempt] | None:
        """领取 Run 并准备 Attempt；抢不到（他人活跃租约 / 已取消）返回 None。"""
        run = await db.get(AgentRun, run_id)
        if run is None:
            return None
        if run.cancel_requested:
            return None
        status = RunStatus(run.status)
        if status == RunStatus.QUEUED:
            attempt = await repo.begin_attempt(run_id)
            if not await repo.claim_lease(run_id, worker_id, self._lease_seconds):
                return None
            return run, attempt
        if status == RunStatus.RUNNING:
            # 无租约 RUNNING（API resume / begin_attempt 后崩溃）：新 Attempt 已
            # 就绪，直接沿用当前 open Attempt，不再 pause+重建（避免每次用户恢复
            # 都多出一个暂停 Attempt）。
            had_lease = run.lease_expires_at is not None
            if not await repo.claim_lease(run_id, worker_id, self._lease_seconds):
                return None
            if not had_lease:
                attempt = await self._current_open_attempt(db, run_id)
                if attempt is not None:
                    return run, attempt
                # 异常兜底：无 open Attempt 时走暂停重建路径。
                if not await repo.pause(run_id, worker_id):
                    return None
                attempt = await repo.begin_attempt(run_id, resumed=True)
                if not await repo.claim_lease(run_id, worker_id, self._lease_seconds):
                    return None
                return run, attempt
            # 过期租约接管：pause 收口旧 Attempt → 新建 Attempt → 重新领取。
            if not await repo.pause(run_id, worker_id):
                return None
            attempt = await repo.begin_attempt(run_id, resumed=True)
            if not await repo.claim_lease(run_id, worker_id, self._lease_seconds):
                return None
            return run, attempt
        return None

    async def _current_open_attempt(
        self, db: AsyncSession, run_id: str
    ) -> AgentRunAttempt | None:
        """返回当前尚未收尾的 Attempt（``ended_at IS NULL``），无则 None。"""
        return await db.scalar(
            select(AgentRunAttempt)
            .where(
                AgentRunAttempt.run_id == run_id,
                AgentRunAttempt.ended_at.is_(None),
            )
            .order_by(AgentRunAttempt.attempt.desc())
            .limit(1)
        )

    # ------------------------------------------------------------------ #
    # 会话 / 收口
    # ------------------------------------------------------------------ #

    async def _build_messages(self, db: AsyncSession, run: AgentRun) -> list[ChatMessage]:
        """引擎初始对话：优先用 Run 关联的用户输入消息，回退到会话最近用户消息。"""
        if run.input_message_id is not None:
            message = await db.get(AgentMessage, run.input_message_id)
            if message is not None:
                return [ChatMessage(role=message.role, content=message.content)]
        latest = await db.scalar(
            select(AgentMessage)
            .where(
                AgentMessage.session_id == run.session_id,
                AgentMessage.role == "user",
            )
            .order_by(AgentMessage.sequence.desc())
            .limit(1)
        )
        if latest is not None:
            return [ChatMessage(role="user", content=latest.content)]
        return [ChatMessage(role="user", content="")]

    async def _finalize_failed(
        self, db: AsyncSession, repo: AgentRunRepository, run_id: str, worker_id: str
    ) -> None:
        """执行器错误收口：持有租约经 transition，否则回退系统级 force_fail。"""
        try:
            await repo.transition(run_id, RunStatus.FAILED, worker_id=worker_id)
        except InvalidRunTransition:
            try:
                await repo.force_fail(run_id, error_code="executor_error")
            except Exception:
                logger.exception("force_fail failed for run %s", run_id)


__all__ = ["AgentRunExecutor"]
