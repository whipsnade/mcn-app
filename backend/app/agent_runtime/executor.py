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

**取消待处理孤儿收口**（I1）：``cancel_requested`` 且租约过期/为空的
running/reviewing Run 是"API 写入取消后进程崩溃"的残留——不恢复模型执行，
直接释放 Draft working head 并经终态事务边界收口 cancelled（恰好一个
``run.cancelled``），避免永久卡在中间态并阻塞会话后续任务。

**Utility 终态触发**（§6.4）：用户主 Run（session_analyst_v1）到达终态/
澄清等待（completed/failed/cancelled/clarification_requested）且 settle
成功后，异步 best-effort 触发 run_summary + suggestions（fire-and-forget，
不侵入 settle_terminal 事务边界，失败不影响 Run 状态与事件流）；内部 Run
与 kol_detail_v1 等辅助 Run 不触发。
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
from app.agent_runtime.reviewer import release_run_drafts
from app.agent_runtime.state import TERMINAL_RUN_STATUSES, RunStatus
from app.agent_runtime.tools.factory import load_channel_permissions
from app.agent_runtime.transcript import RunTranscriptLoader
from app.agent_runtime.utility import UtilityDispatcher

logger = logging.getLogger(__name__)

# 会话主分析 Run 的 Profile：Task 19 messages 建 Run 与 Utility 终态触发都用
# 它识别「用户主 Run」；kol_detail_v1 等辅助 Profile 不算。
SESSION_ANALYST_PROFILE = "session_analyst_v1"

# Utility 触发状态（§6.4）：终态 + 澄清等待；paused 不触发（Run 未收口）。
_UTILITY_TRIGGER_STATUSES = frozenset(
    {
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.CLARIFICATION_REQUESTED,
    }
)


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
        utility_dispatcher: UtilityDispatcher | None = None,
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
        # §6.4：Run 终态后的 best-effort utility 触发器；None（窄装配）时跳过。
        self._utility_dispatcher = utility_dispatcher
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

        没有可执行 Run 时再检查取消待处理孤儿（``cancel_requested`` 且租约
        过期/为空的 running/reviewing Run，I1）：不恢复模型执行，直接收口
        cancelled 并返回其 run_id。已停止（``stop`` 后）的 executor 不再
        领取任何新 Run。
        """
        if self._stop.is_set():
            return None
        async with self._session_factory() as db:
            run_id = await self._find_claimable_id(db)
            if run_id is not None:
                await self._process_run(db, run_id, self._worker_id)
                return run_id
            cancel_pending_id = await self._find_cancel_pending_id(db)
            if cancel_pending_id is None:
                return None
            run = await db.get(AgentRun, cancel_pending_id)
            if run is not None and await self._settle_cancel_requested(db, run):
                return cancel_pending_id
            return None

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
            # §6.4：settle 已成功提交后再异步触发 utility（summary/suggestions），
            # 不侵入 settle_terminal 事务边界。
            self._schedule_run_utilities(run, outcome.status)
            return outcome.status
        except asyncio.CancelledError:
            # 关闭/取消：不强行收口，交给恢复循环在租约过期后接管。
            raise
        except Exception:
            logger.exception("agent run %s failed in executor", run_id)
            # 回滚引擎崩溃现场的半提交写入，兜底收口在干净状态上进行（H1）。
            await db.rollback()
            await self._finalize_failed(db, run_id, worker_id)
            await db.commit()
            # 兜底收口成功（或引擎已自行收口终态）时同样触发 utility；收口被
            # 他人租约拦截（A4）时 Run 仍非终态，门控自然跳过。
            settled = await db.get(AgentRun, run_id)
            if settled is not None:
                self._schedule_run_utilities(settled, RunStatus(settled.status))
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

    async def _find_cancel_pending_id(self, db: AsyncSession) -> str | None:
        """返回一个取消待处理孤儿 Run id（I1）：``cancel_requested`` 且租约
        过期/为空的 running/reviewing Run。

        这类 Run 的原 worker 已崩溃，永远不会再走到引擎的取消检查点；必须由
        执行器/恢复循环收口，否则永久停在 running/reviewing 并阻塞会话后续
        任务（单活动 Run 约束把它算作活动）。租约仍活跃的不在此列——在途
        worker 的取消检查点会自行收口，本扫描不抢（A4 语义）。
        """
        now = utc_now()
        return await db.scalar(
            select(AgentRun.id)
            .where(
                AgentRun.run_kind == "user",
                AgentRun.status.in_((RunStatus.RUNNING, RunStatus.REVIEWING)),
                AgentRun.cancel_requested.is_(True),
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
            # I1：扫描与领取间隙到达的取消（或恢复循环 process_run 直传的取消
            # 孤儿）：不恢复模型执行，直接经终态事务边界收口 cancelled。
            await self._settle_cancel_requested(db, run)
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

    async def settle_cancel_requested(self, run_id: str) -> bool:
        """取消待处理孤儿 Run 的收口入口（恢复循环调用，I1）。

        返回 ``True`` 表示本次完成收口（迁移 cancelled 并发恰好一个
        ``run.cancelled`` 终态事件）；``False`` 表示无需/不应由本实例收口
        （Run 不存在、取消信号消失、已终态、已有终态事件被并发收口，或租约
        被他人活跃持有——在途 worker 的取消检查点会自行收口，不抢）。
        """
        async with self._session_factory() as db:
            run = await db.get(AgentRun, run_id)
            if run is None or not run.cancel_requested:
                return False
            return await self._settle_cancel_requested(db, run)

    async def _settle_cancel_requested(self, db: AsyncSession, run: AgentRun) -> bool:
        """取消待处理孤儿 Run 的收口（I1）：释放 Draft working head（idle）后
        经 H1 终态事务边界迁移 cancelled，发恰好一个 ``run.cancelled`` 终态事件。

        与引擎 ``_settle_cancelled`` 同一语义、同一终态路径
        （``AgentEventStream.settle_terminal``，不新造终态出口）：行锁串行化
        保证与 API/引擎的并发取消收口恰好一个终态事件，后到者幂等返回。
        租约仍被他人活跃持有时不抢（A4 语义），返回 ``False`` 交给在途 worker。
        reviewing 孤儿沿用在线取消口径：Draft 置 idle 释放，不套用 reject 的
        整批 failed 清理（与引擎 ``_settle_cancelled`` 一致）。
        """
        if RunStatus(run.status) in TERMINAL_RUN_STATUSES:
            return False
        now = utc_now()
        if run.lease_expires_at is not None and run.lease_expires_at > now:
            return False
        await release_run_drafts(db, run.id, outcome="idle")
        event = await AgentEventStream(db, self._broker).settle_terminal(
            run.id, run.user_id, RunStatus.CANCELLED, {}
        )
        if event is not None:
            # §6.4：取消收口成功后 best-effort 触发 utility（幂等后到者不触发）。
            self._schedule_run_utilities(run, RunStatus.CANCELLED)
        return event is not None

    def _schedule_run_utilities(self, run: AgentRun, status: RunStatus) -> None:
        """Run 终态后的 best-effort utility 触发（§6.4）：run_summary + suggestions。

        只触发用户主分析 Run（run_kind/visibility=user 且
        profile=session_analyst_v1）：内部 Run（Utility/Reviewer）与
        kol_detail_v1 等辅助 Run 不触发。触发是 fire-and-forget 异步任务，
        任何失败不影响 Run 状态与事件流。
        """
        if self._utility_dispatcher is None or status not in _UTILITY_TRIGGER_STATUSES:
            return
        if (
            run.run_kind != "user"
            or run.visibility != "user"
            or run.profile_name != SESSION_ANALYST_PROFILE
        ):
            return
        self._utility_dispatcher.schedule_run_followups(run_id=run.id)

    async def _finalize_failed(
        self, db: AsyncSession, run_id: str, worker_id: str
    ) -> None:
        """执行器错误收口：经 settle_terminal 事务边界把 Run 置 failed 并发
        恰好一个 run.failed 终态事件（稳定 ``error_code=executor_error``，
        §5.8/G1）——引擎在 decide 之外崩溃时没人发终态事件，SSE 流会因此
        不结束、前端 Run 卡停在中间态。

        迁移与事件在同一加锁事务提交（H1）：持租约走状态机、无租约走系统级
        force_fail；已是终态（引擎已自行收口，含已发 run.failed）或租约被
        他人活跃持有（已接管，终态事件由接管方负责，A4 闸门）时幂等返回，
        绝不重复发送。收口本身失败只记日志——Run 保持 running + 租约，
        恢复循环在租约过期后接管自愈。
        """
        try:
            run = await db.get(AgentRun, run_id)
            if run is None:  # pragma: no cover - Run 必然存在
                return
            await AgentEventStream(db, self._broker).settle_terminal(
                run_id,
                run.user_id,
                RunStatus.FAILED,
                {"outcome": "failed", "error_code": "executor_error"},
                worker_id=worker_id,
            )
        except Exception:
            logger.exception("run %s terminal settlement failed in executor", run_id)


__all__ = ["AgentRunExecutor"]
