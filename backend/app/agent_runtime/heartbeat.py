"""Run 租约心跳（v3 加固 §5.5 / A4）。

``RunLeaseHeartbeat`` 在 Run 执行期间用**独立 DB Session** 每
``lease_seconds / 3`` 续租一次，覆盖 decide（长模型调用）、MCP 外发与
Reviewer 复核全程——此前只在决策循环顶续租，单次长迭代超过租约时长就会被
恢复循环误判死亡（双 worker 并发窗口 / Run 以 ``run_lease_lost`` 误失败）。

心跳发现租约已被其他 worker 接管（renew 明确失败）时置 ``lost``：旧 worker
必须停止，且不得再发布 Artifact 或写 Run 终态（引擎在发布/终态写入前会
再次确认租约持有，见 ``AgentEngine`` 的检查点）。瞬时异常（DB 抖动）不判死，
记日志后下轮重试。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.repository import AgentRunRepository

logger = logging.getLogger(__name__)

# 会话工厂类型：生产为 SessionFactory（独立连接真实提交）；测试可注入共享会话。
SessionFactoryLike = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class RunLeaseHeartbeat:
    """Run 执行期间的租约心跳：独立 DB Session 周期续租 + 接管检测。"""

    def __init__(
        self,
        *,
        session_factory: SessionFactoryLike,
        run_id: str,
        worker_id: str,
        lease_seconds: int,
        interval_seconds: float | None = None,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds_must_be_positive")
        self._session_factory = session_factory
        self._run_id = run_id
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        # 每 lease_seconds/3 续租一次：单次长调用期间至少有两次续租窗口。
        self._interval_seconds = (
            interval_seconds if interval_seconds is not None else lease_seconds / 3
        )
        self._lost = False
        self._task: asyncio.Task[None] | None = None

    @property
    def lost(self) -> bool:
        """租约已被其他 worker 接管（renew 明确失败）。"""
        return self._lost

    async def start(self) -> None:
        """启动心跳（幂等：重复 start 不重建任务）。"""
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """停止心跳（幂等：未 start 或重复 stop 都安全）。"""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    # ------------------------------------------------------------------ #

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval_seconds)
            if not await self._renew_once():
                self._lost = True
                logger.warning(
                    "run %s lease heartbeat lost; worker %s must stop",
                    self._run_id,
                    self._worker_id,
                )
                return

    async def _renew_once(self) -> bool:
        """续租一次；明确拒绝（他人接管）返回 False，瞬时异常记日志并下轮重试。"""
        try:
            async with self._session_factory() as db:
                renewed = await AgentRunRepository(db).renew_lease(
                    self._run_id, self._worker_id, self._lease_seconds
                )
                await db.commit()
                return renewed
        except asyncio.CancelledError:
            raise
        except Exception:
            # DB 抖动不判死：下轮重试；真被接管时下轮 renew 会明确返回 False。
            logger.warning("lease heartbeat renew failed", exc_info=True)
            return True


__all__ = ["RunLeaseHeartbeat"]
