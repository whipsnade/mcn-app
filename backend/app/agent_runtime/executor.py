"""进程级 Agent Run 执行器（设计文档 §四 / §七 / Task 15；v3 加固 §5.4/§5.5）。

``AgentRunExecutor`` 是 TaskRunner 等价的新执行器：一个后台 worker 循环按固定
间隔扫描可领取的 Run（queued 新任务 + 过期租约的 running/reviewing Run），
领取后开启/重建 ``agent_run_attempts`` 并调用 Task 14 ``AgentEngine.run``
执行到终态。

**过期租约接管**（§七「恢复时从最后一个完整 Step 继续」）：租约过期的
running Run 可被新 worker 领取。领取后先 ``pause`` 收口旧 Attempt
（outcome=paused），再经 ``begin_attempt(resumed=True)`` 开启全新 Attempt
（决策/时长计数从零），最后重新领取租约。引擎的 ``_next_step_sequence``
从 ``agent_steps`` 现存最大 sequence 继续，因此完整 Step 绝不重放、
sequence 不归零；``RunTranscriptLoader``（§5.4）重建的对话上下文包含此前
工具结果，模型不会重复调用已 settled 的 MCP 工具。

**reviewing 接管**（§5.5）：复核期间崩溃的 reviewing Run（租约过期）沿用
open Attempt 被领取，保持 reviewing 状态交给引擎继续未完成的复核——已
approve 的 Review Item 不重审，pending/revise 继续，完成后走原子发布。

**优雅关闭**：``stop()`` 置停止信号，循环只在下一次迭代开始前检查，因此当前
in-flight Run 会在事务安全点自然完成；超时则取消循环任务，留下的 running Run
由恢复循环在租约过期后接管。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.engine import AgentEngine
from app.agent_runtime.events import AgentEventBroker, AgentEventStream
from app.agent_runtime.models import AgentRun, AgentRunAttempt
from app.agent_runtime.profiles import get_profile
from app.agent_runtime.repository import AgentRunRepository, utc_now
from app.agent_runtime.state import InvalidRunTransition, RunStatus
from app.agent_runtime.tools.factory import load_channel_permissions
from app.agent_runtime.transcript import RunTranscriptLoader

logger = logging.getLogger(__name__)


class AgentRunExecutor:
    """进程内执行器：后台循环领取 queued / 过期租约 Run 并驱动引擎执行。

    ``session_factory`` 必须是一个可异步上下文管理的会话工厂
    （``async with session_factory() as db`` 产生 :class:`AsyncSession`）；
    ``engine_factory`` 接收 ``(会话, worker_id, channel_permissions=...)`` 并
    返回绑定该会话、租约 worker 与用户渠道权限的 :class:`AgentEngine`
    （渠道权限由执行器按 Run 的 ``user_id`` 实时查询注入，设计 §5.1）。
    测试可注入 ``lambda: <共享 AsyncSession 的上下文管理器>``。
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        engine_factory: Callable[..., AgentEngine],
        worker_id: str,
        lease_seconds: int = 300,
        claim_interval_seconds: float = 1.0,
        broker: AgentEventBroker | None = None,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds_must_be_positive")
        self._session_factory = session_factory
        self._engine_factory = engine_factory
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._claim_interval_seconds = claim_interval_seconds
        # 终态事件广播（G1）：生产必须注入与 API 共享的进程级 broker，同进程
        # SSE 订阅才能即时收到 executor 补发的 run.failed；缺省独立 broker
        # （测试注入）时事件仍持久化，跨实例 reader 靠 DB 轮询兜底。
        self._broker = broker if broker is not None else AgentEventBroker()
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
        run, attempt, resumed_by = prepared
        try:
            # §5.4：从触发消息 + 完整 Step 重建对话上下文（含此前工具结果与
            # 待复用的崩溃残留 Step），接管后模型不重复调用已 settled 工具。
            transcript = await RunTranscriptLoader(db).load(run)
            channel_permissions = await load_channel_permissions(db, run.user_id)
            engine = self._engine_factory(
                db, worker_id, channel_permissions=channel_permissions
            )
            outcome = await engine.run(
                run=run,
                attempt_id=attempt.id,
                profile=get_profile(run.profile_name),
                messages=transcript.messages,
                # G3：显式用户问题锚点（触发消息 / prompt_snapshot 触发上下文），
                # 引擎不再从消息列表尾部反推（尾部是 tool_result 回放）。
                user_question=transcript.user_question,
                # §5.8/§10.5：用户可见 Run 注入 thinking sink，主 Agent 真实
                # thinking 实时 SSE；内部 Run（Reviewer/Utility）为 None 不注入。
                thinking_sink=engine.thinking_sink_for(run),
                resume_step=transcript.resume_step,
                resumed_by=resumed_by,
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
        """返回一个可领取的 user Run id：queued 新任务优先，其次过期/无租约的活动 Run。

        无租约 RUNNING（``lease_expires_at`` 为 NULL）是 API resume 经
        ``begin_attempt(resumed=True)`` 或 begin_attempt 后崩溃留下的状态：
        当前 Attempt 已就绪、无活跃 worker 持有，必须可被领取，否则恢复后
        的 Run 会永久卡在 running。租约过期的 REVIEWING Run（复核期间崩溃，
        §5.5）同样可领取，由引擎继续未完成的复核。
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
                AgentRun.status.in_((RunStatus.RUNNING, RunStatus.REVIEWING)),
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
    ) -> tuple[AgentRun, AgentRunAttempt, str | None] | None:
        """领取 Run 并准备 Attempt；抢不到（他人活跃租约 / 已取消）返回 None。

        返回 ``(run, attempt, resumed_by)``：``resumed_by`` 区分本次执行是
        首次启动（None）、用户主动 resume 后的领取（``"user"``）还是系统
        接管（``"system"``），供 ``run.resumed`` 事件归因。
        """
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
            return run, attempt, None
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
                    return run, attempt, "user"
                # 异常兜底：无 open Attempt 时走暂停重建路径。
                if not await repo.pause(run_id, worker_id):
                    return None
                attempt = await repo.begin_attempt(run_id, resumed=True)
                if not await repo.claim_lease(run_id, worker_id, self._lease_seconds):
                    return None
                return run, attempt, "system"
            # 过期租约接管：pause 收口旧 Attempt → 新建 Attempt → 重新领取。
            if not await repo.pause(run_id, worker_id):
                return None
            attempt = await repo.begin_attempt(run_id, resumed=True)
            if not await repo.claim_lease(run_id, worker_id, self._lease_seconds):
                return None
            return run, attempt, "system"
        if status == RunStatus.REVIEWING:
            # reviewing 接管（§5.5 复核期间崩溃）：沿用 open Attempt 并保持
            # reviewing 状态，引擎继续未完成的复核（已 approve 的 Item 不重审）。
            # transition→reviewing 不收口 Attempt，open Attempt 必然存在；
            # 缺失属不变量破坏，记日志跳过（不静默重建破坏复核审计）。
            if not await repo.claim_lease(run_id, worker_id, self._lease_seconds):
                return None
            attempt = await self._current_open_attempt(db, run_id)
            if attempt is None:
                logger.warning("reviewing run %s has no open attempt; skipping", run_id)
                return None
            return run, attempt, "system"
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
    # 收口
    # ------------------------------------------------------------------ #

    async def _finalize_failed(
        self, db: AsyncSession, repo: AgentRunRepository, run_id: str, worker_id: str
    ) -> None:
        """执行器错误收口：持有租约经 transition，否则回退系统级 force_fail。

        只有本 worker 真实把 Run 置 failed（transition 成功或 force_fail 确认
        无其他活跃持有者）才发 run.failed 终态事件（稳定
        ``error_code=executor_error``，§5.8/G1）——引擎在 decide 之外崩溃时
        没人发终态事件，SSE 流会因此不结束、前端 Run 卡停在中间态。
        已是终态（引擎已自行收口，含已发 run.failed）或租约被他人活跃持有
        （已接管，终态事件由接管方负责，A4 闸门）时跳过，绝不重复发送。
        """
        try:
            await repo.transition(run_id, RunStatus.FAILED, worker_id=worker_id)
            failed = True
        except InvalidRunTransition:
            try:
                failed = await repo.force_fail(run_id, error_code="executor_error")
            except Exception:
                logger.exception("force_fail failed for run %s", run_id)
                failed = False
        if not failed:
            return
        run = await db.get(AgentRun, run_id)
        if run is None:  # pragma: no cover - 刚收口的 Run 必然存在
            return
        try:
            await AgentEventStream(db, self._broker).append_terminal_once(
                run_id,
                run.user_id,
                "run.failed",
                {"outcome": "failed", "error_code": "executor_error"},
            )
        except Exception:
            # 事件补发失败不改变已落库的 failed 终态；恢复/重读仍以 DB 为准。
            logger.exception("run.failed event append failed for run %s", run_id)


__all__ = ["AgentRunExecutor"]
