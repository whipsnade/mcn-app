"""Agent Run 恢复循环（设计文档 §11.1 / §5.3 / §5.4 / §七 / Task 15）。

``RecoveryLoop`` 周期性扫描三类积压：

1. **过期租约 Run**：running/reviewing 且租约已过期的 Run 重新提交给
   :class:`AgentRunExecutor`（新 worker 领取 + 新建/沿用 Attempt + 引擎继续），
   避免进程崩溃后卡死；reviewing Run 由引擎继续未完成的复核（§5.5）。
   其中 ``cancel_requested=True`` 的 Run 是"API 写入取消后进程崩溃"的孤儿
   （I1）：**不恢复模型执行**，直接经执行器取消收口路径落 cancelled 终态
   （恰好一个 ``run.cancelled``），否则永久卡在中间态并阻塞会话后续任务；
2. **stuck running/reserved MCP 调用**：超过受控时间（``stuck_seconds``，配置
   ``AGENT_TOOL_CALL_STUCK_SECONDS``）仍处于 ``running``/``reserved`` 的调用，
   先**迁移为 unknown**（保留预留，绝不直接释放或重新外发，§5.4），再交给
   只读核对；``started_at`` 为超时判断依据，NULL（旧两阶段流程崩溃残留）按
   stuck 处理；
3. **unknown MCP 调用**：``agent_tool_calls.status == 'unknown'`` 的调用经
   Task 8 ``AgentMcpTool.reconcile(logical_call_id)`` 按 ``logical_call_id``
   **只读核对**，绝不重发原工具。结果落 ``agent_tool_call_reconciliations``
   （source=``upstream_probe``）：确认成功可回取 payload → 建 Evidence + settle；
   确认成功无 payload → settle + result_unavailable（不建 Evidence）；确认失败 →
   release；无法核对 → 保持 reserved/unknown + ``keep_unknown`` 审计并产生运维告警。

**明确不使用** legacy ``release_expired_unknown`` 的超时自动释放策略：unknown 调用
保持预留，直到人工核对（§11.1「禁止自动重放」）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.executor import AgentRunExecutor
from app.agent_runtime.models import (
    AgentRun,
    AgentToolCall,
    AgentToolCallReconciliation,
)
from app.agent_runtime.repository import utc_now
from app.agent_runtime.state import RunStatus
from app.agent_runtime.tools.mcp import RESULT_UNKNOWN, AgentMcpTool

logger = logging.getLogger(__name__)

# 单轮扫描上限：避免一次恢复把全部 unknown 调用都拉进内存。
_SCAN_LIMIT = 200


class RecoveryLoop:
    """周期性恢复扫描器；``clock`` 可注入确定性时钟做过期断言。"""

    def __init__(
        self,
        *,
        executor: AgentRunExecutor,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        tool_factory: Callable[[AsyncSession, AgentToolCall], AgentMcpTool | None],
        worker_id: str,
        lease_seconds: int = 300,
        interval_seconds: float = 30.0,
        clock: Callable[[], datetime] = utc_now,
        stuck_seconds: float = 900.0,
    ) -> None:
        self._executor = executor
        self._session_factory = session_factory
        self._tool_factory = tool_factory
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._interval_seconds = interval_seconds
        self._clock = clock
        self._stuck_seconds = stuck_seconds
        self._stop = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """启动后台恢复循环（幂等）。"""
        if self._loop_task is None:
            self._loop_task = asyncio.create_task(self._run_loop())

    async def stop(self, *, timeout_seconds: float = 5) -> None:
        self._stop.set()
        if self._loop_task is not None:
            try:
                await asyncio.wait_for(self._loop_task, timeout=timeout_seconds)
            except TimeoutError:
                self._loop_task.cancel()
                await asyncio.gather(self._loop_task, return_exceptions=True)
            self._loop_task = None

    async def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("agent recovery loop iteration failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval_seconds)
            except TimeoutError:
                pass

    # ------------------------------------------------------------------ #
    # 单轮扫描
    # ------------------------------------------------------------------ #

    async def run_once(self) -> tuple[
        tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]
    ]:
        """执行一轮恢复；返回 ``(已接管重建/取消收口的 run_ids, stuck 迁移
        logical_call_ids, 已核对 logical_call_ids, 运维告警 ids)``。"""
        reclaimed = await self.reclaim_expired_runs()
        stuck = await self.migrate_stuck_tool_calls()
        reconciled, warnings = await self.reconcile_unknown_calls()
        return reclaimed, stuck, reconciled, warnings

    # ------------------------------------------------------------------ #
    # 过期租约 Run 恢复
    # ------------------------------------------------------------------ #

    async def reclaim_expired_runs(self) -> tuple[str, ...]:
        """扫描可接管的 running/reviewing Run，重新提交给执行器；并收口取消
        待处理孤儿 Run（I1）。

        与执行器 ``_find_claimable_id`` 一致：租约过期或无租约（NULL）的
        running Run 都可接管——NULL 租约是 API resume 经
        ``begin_attempt(resumed=True)`` 留下的状态，执行器停止时若恢复循环
        也不接管会永久卡在 running。租约过期的 reviewing Run（复核期间崩溃，
        §5.5）同样接管，由引擎继续未完成的复核。

        I1：``cancel_requested=True`` 且租约过期/为空的 Run 不交给执行器
        恢复模型执行——其原 worker 已崩溃、永远不会走到取消检查点；直接经
        执行器的取消收口路径（释放 Draft + ``settle_terminal`` CANCELLED）
        落 cancelled 终态与恰好一个 ``run.cancelled``，避免永久卡在
        running/reviewing 并阻塞会话后续任务。租约仍活跃的不在本扫描内
        （在途 worker 的取消检查点会自行收口，不抢）。

        返回本轮处理的 run_ids（接管重建 + 取消收口）。
        """
        reclaimed: list[str] = []
        async with self._session_factory() as db:
            expired = (
                await db.scalars(
                    select(AgentRun.id)
                    .where(
                        AgentRun.run_kind == "user",
                        AgentRun.status.in_((RunStatus.RUNNING, RunStatus.REVIEWING)),
                        AgentRun.cancel_requested.is_(False),
                        or_(
                            AgentRun.lease_expires_at.is_(None),
                            AgentRun.lease_expires_at <= self._clock(),
                        ),
                    )
                    .order_by(AgentRun.id.asc())
                    .limit(_SCAN_LIMIT)
                )
            ).all()
            cancel_pending = (
                await db.scalars(
                    select(AgentRun.id)
                    .where(
                        AgentRun.run_kind == "user",
                        AgentRun.status.in_((RunStatus.RUNNING, RunStatus.REVIEWING)),
                        AgentRun.cancel_requested.is_(True),
                        or_(
                            AgentRun.lease_expires_at.is_(None),
                            AgentRun.lease_expires_at <= self._clock(),
                        ),
                    )
                    .order_by(AgentRun.id.asc())
                    .limit(_SCAN_LIMIT)
                )
            ).all()
        for run_id in expired:
            try:
                # 使用恢复循环自己的 worker id：与原 worker 租约隔离，避免同一
                # Run 被两个 worker 并发执行（Fix 4）。
                await self._executor.process_run(run_id, worker_id=self._worker_id)
                reclaimed.append(run_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("recovery reclaim failed for run %s", run_id)
        for run_id in cancel_pending:
            try:
                # 取消孤儿：不恢复模型执行，直接 settle cancelled（I1）。
                if await self._executor.settle_cancel_requested(run_id):
                    reclaimed.append(run_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("recovery cancel-settle failed for run %s", run_id)
        return tuple(reclaimed)

    # ------------------------------------------------------------------ #
    # stuck running/reserved MCP 调用迁移（§5.4）
    # ------------------------------------------------------------------ #

    async def migrate_stuck_tool_calls(self) -> tuple[str, ...]:
        """超过受控时间仍处于 ``running``/``reserved`` 的调用迁移为 ``unknown``。

        以 ``started_at`` 为超时判断依据（阈值 ``stuck_seconds``，配置
        ``AGENT_TOOL_CALL_STUCK_SECONDS``）；``started_at`` 为 NULL 的
        ``reserved`` 行只可能来自旧两阶段流程的崩溃窗口，按 stuck 处理。
        迁移只改状态、保留预留——**绝不直接释放或重新外发**，随后由
        :meth:`reconcile_unknown_calls` 走既有只读核对。

        迁移用 ``SELECT ... FOR UPDATE`` + 状态谓词守护：与在途调用的
        finalize 竞态时，已终态（settled/failed）的行不会被翻回 unknown。
        """
        threshold = self._clock() - timedelta(seconds=self._stuck_seconds)
        now = self._clock()
        migrated: list[str] = []
        async with self._session_factory() as db:
            stuck = list(
                (
                    await db.execute(
                        select(AgentToolCall.id, AgentToolCall.logical_call_id)
                        .where(
                            AgentToolCall.status.in_(("running", "reserved")),
                            or_(
                                AgentToolCall.started_at.is_(None),
                                AgentToolCall.started_at <= threshold,
                            ),
                        )
                        .order_by(AgentToolCall.started_at.asc())
                        .limit(_SCAN_LIMIT)
                    )
                ).all()
            )
            for call_id, logical_call_id in stuck:
                row = await db.scalar(
                    select(AgentToolCall)
                    .where(
                        AgentToolCall.id == call_id,
                        AgentToolCall.status.in_(("running", "reserved")),
                    )
                    .with_for_update()
                )
                if row is None:
                    continue  # 扫描与并发 finalize 竞态：已终态的行不得翻回 unknown
                row.status = "unknown"
                row.error_type = RESULT_UNKNOWN
                row.safe_error_message = "stuck running/reserved beyond controlled window"
                row.completed_at = now
                migrated.append(logical_call_id)
            await db.commit()
        return tuple(migrated)

    # ------------------------------------------------------------------ #
    # unknown MCP 调用恢复核对
    # ------------------------------------------------------------------ #

    async def reconcile_unknown_calls(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """核对所有 ``status == 'unknown'`` 的 MCP 调用（只读核对，绝不重放）。

        返回 ``(已核对 logical_call_ids, 无法核对产生运维告警的 ids)``。核对结果
        （settle/release/keep_unknown）由 ``AgentMcpTool.reconcile`` 落库并写
        ``agent_tool_call_reconciliations`` 审计行。
        """
        reconciled: list[str] = []
        warnings: list[str] = []
        async with self._session_factory() as db:
            unknown = (
                await db.scalars(
                    select(AgentToolCall)
                    .where(AgentToolCall.status == "unknown")
                    .order_by(AgentToolCall.started_at.asc())
                    .limit(_SCAN_LIMIT)
                )
            ).all()
            for call in unknown:
                try:
                    tool = self._tool_factory(db, call)
                except Exception:
                    logger.exception(
                        "recovery tool factory failed for call %s", call.logical_call_id
                    )
                    continue
                if tool is None:
                    continue
                try:
                    # 记录核对前审计状态；只有核对改变了状态才发运维告警，
                    # 避免每轮扫描对同一 unconfirmable 调用重复告警。
                    before = await self._last_reconciliation_decision(db, call.id)
                    result = await tool.reconcile(call.logical_call_id)
                    after = await self._last_reconciliation_decision(db, call.id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("reconcile failed for call %s", call.logical_call_id)
                    continue
                reconciled.append(call.logical_call_id)
                if result.status == "unknown" and after != before:
                    # 无法核对且状态首次确认：保持预留并产生运维告警（不自动释放）。
                    logger.warning(
                        "agent_tool_call unconfirmable logical_call_id=%s note=%s",
                        call.logical_call_id,
                        result.safe_summary,
                    )
                    warnings.append(call.logical_call_id)
            # 核对结果（settle/release/evidence/审计）必须提交：否则整轮核对在
            # 生产环境静默回滚（会话在 SELECT 后 autobegin，close 时回滚）。
            await db.commit()
        return tuple(reconciled), tuple(warnings)

    @staticmethod
    async def _last_reconciliation_decision(
        db: AsyncSession, tool_call_id: str
    ) -> str | None:
        return await db.scalar(
            select(AgentToolCallReconciliation.decision)
            .where(AgentToolCallReconciliation.tool_call_id == tool_call_id)
            .order_by(
                AgentToolCallReconciliation.created_at.desc(),
                AgentToolCallReconciliation.id.desc(),
            )
            .limit(1)
        )


__all__ = ["RecoveryLoop"]
