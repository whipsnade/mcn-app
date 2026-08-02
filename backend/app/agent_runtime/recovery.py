"""Agent Run 恢复循环（设计文档 §11.1 / §七 / Task 15）。

``RecoveryLoop`` 周期性扫描两类积压：

1. **过期租约 Run**：running 且租约已过期的 Run 重新提交给 :class:`AgentRunExecutor`
   （新 worker 领取 + 新建 Attempt + 引擎继续），避免进程崩溃后卡死；
2. **unknown MCP 调用**：``agent_tool_calls.status == 'unknown'`` 的调用经
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
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.executor import AgentRunExecutor
from app.agent_runtime.models import AgentRun, AgentToolCall
from app.agent_runtime.repository import utc_now
from app.agent_runtime.state import RunStatus
from app.agent_runtime.tools.mcp import AgentMcpTool

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
    ) -> None:
        self._executor = executor
        self._session_factory = session_factory
        self._tool_factory = tool_factory
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._interval_seconds = interval_seconds
        self._clock = clock
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

    async def run_once(self) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        """执行一轮恢复；返回 ``(已领取重建的 run_ids, 已核对 logical_call_ids, 运维告警 ids)``。"""
        reclaimed = await self.reclaim_expired_runs()
        reconciled, warnings = await self.reconcile_unknown_calls()
        return reclaimed, reconciled, warnings

    # ------------------------------------------------------------------ #
    # 过期租约 Run 恢复
    # ------------------------------------------------------------------ #

    async def reclaim_expired_runs(self) -> tuple[str, ...]:
        """扫描租约已过期的 running Run，重新提交给执行器（新 Attempt + 引擎继续）。"""
        reclaimed: list[str] = []
        async with self._session_factory() as db:
            expired = (
                await db.scalars(
                    select(AgentRun.id)
                    .where(
                        AgentRun.run_kind == "user",
                        AgentRun.status == RunStatus.RUNNING,
                        AgentRun.cancel_requested.is_(False),
                        AgentRun.lease_expires_at.isnot(None),
                        AgentRun.lease_expires_at <= self._clock(),
                    )
                    .order_by(AgentRun.id.asc())
                    .limit(_SCAN_LIMIT)
                )
            ).all()
        for run_id in expired:
            try:
                await self._executor.process_run(run_id)
                reclaimed.append(run_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("recovery reclaim failed for run %s", run_id)
        return tuple(reclaimed)

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
                    result = await tool.reconcile(call.logical_call_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("reconcile failed for call %s", call.logical_call_id)
                    continue
                reconciled.append(call.logical_call_id)
                if result.status == "unknown":
                    # 无法核对：保持预留并产生运维告警（不自动释放）。
                    logger.warning(
                        "agent_tool_call unconfirmable logical_call_id=%s note=%s",
                        call.logical_call_id,
                        result.safe_summary,
                    )
                    warnings.append(call.logical_call_id)
        return tuple(reconciled), tuple(warnings)


__all__ = ["RecoveryLoop"]
