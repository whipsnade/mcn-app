from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.models import AgentRun, AgentRunAttempt
from app.agent_runtime.state import (
    InvalidRunTransition,
    TERMINAL_RUN_STATUSES,
    RunStatus,
    ensure_transition,
)

# 单次 Attempt 的时长 / 决策数保护阈值（30 分钟 / 50 次模型决策）。
ATTEMPT_MAX_SECONDS = 30 * 60
ATTEMPT_MAX_DECISIONS = 50


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class AgentRunRepository:
    """统一 Agent Run 状态机 + Attempt 生命周期 + 租约的持久化层。

    30 分钟 / 50 决策保护按 Run Attempt 计算：首次启动和每次用户恢复都会
    新建一条 ``agent_run_attempts``（per-attempt 计数从零），而
    ``agent_runs.decision_count`` 保留跨 Attempt 的累计审计值，绝不重置。
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def lock_run(self, run_id: str) -> AgentRun:
        run = await self.db.scalar(
            select(AgentRun).where(AgentRun.id == run_id).with_for_update()
        )
        if run is None:
            raise LookupError("run_not_found")
        return run

    async def begin_attempt(
        self, run_id: str, *, resumed: bool = False
    ) -> AgentRunAttempt:
        """首次启动（queued → running）或用户恢复（paused → running）时开启新 Attempt。

        新 Attempt 的 ``started_at`` / ``decision_count`` 从零开始；
        ``run.decision_count`` 与 ``run.started_at`` 保留累计审计值。
        """
        run = await self.lock_run(run_id)
        current = RunStatus(run.status)
        if resumed:
            if current != RunStatus.PAUSED:
                raise InvalidRunTransition("run_not_paused")
        elif current != RunStatus.QUEUED:
            raise InvalidRunTransition("run_not_queued")
        ensure_transition(current, RunStatus.RUNNING)
        now = utc_now()
        max_attempt = await self.db.scalar(
            select(func.max(AgentRunAttempt.attempt)).where(
                AgentRunAttempt.run_id == run_id
            )
        )
        attempt = AgentRunAttempt(
            id=str(uuid4()),
            run_id=run_id,
            attempt=(max_attempt or 0) + 1,
            started_at=now,
            ended_at=None,
            decision_count=0,
            outcome="running",
        )
        self.db.add(attempt)
        run.status = RunStatus.RUNNING
        run.started_at = run.started_at or now
        run.paused_at = None
        await self.db.flush()
        return attempt

    async def claim_lease(
        self,
        run_id: str,
        worker_id: str,
        lease_seconds: int,
        *,
        now: datetime | None = None,
    ) -> bool:
        """抢占租约；拒绝终态、clarification 完成态以及他人未过期租约。"""
        now = now or utc_now()
        run = await self.lock_run(run_id)
        current = RunStatus(run.status)
        if current in TERMINAL_RUN_STATUSES or current == RunStatus.CLARIFICATION_REQUESTED:
            return False
        if run.lease_owner is not None and (
            run.lease_expires_at is None or run.lease_expires_at > now
        ):
            return False
        run.lease_owner = worker_id
        run.lease_expires_at = now + timedelta(seconds=lease_seconds)
        run.heartbeat_at = now
        run.status = RunStatus.RUNNING
        run.started_at = run.started_at or now
        run.paused_at = None
        await self.db.flush()
        return True

    async def pause(self, run_id: str, worker_id: str) -> bool:
        """运行达到阈值后暂停：释放租约、结束当前 Attempt（outcome=paused）。"""
        run = await self.lock_run(run_id)
        if not self._owns_active_lease(run, worker_id):
            return False
        ensure_transition(RunStatus(run.status), RunStatus.PAUSED)
        now = utc_now()
        run.status = RunStatus.PAUSED
        run.paused_at = now
        run.lease_owner = None
        run.lease_expires_at = None
        await self._end_open_attempt(run_id, "paused", now)
        await self.db.flush()
        return True

    async def transition(
        self, run_id: str, target: RunStatus, *, worker_id: str
    ) -> AgentRun:
        """按状态机迁移 Run 状态；仅活跃租约持有者可迁移，终态/暂停做 Attempt 收尾。"""
        run = await self.lock_run(run_id)
        ensure_transition(RunStatus(run.status), target)
        if not self._owns_active_lease(run, worker_id):
            raise InvalidRunTransition("run_lease_not_held")
        now = utc_now()
        run.status = target
        if target in TERMINAL_RUN_STATUSES:
            run.completed_at = run.completed_at or now
            run.lease_owner = None
            run.lease_expires_at = None
            await self._end_open_attempt(run_id, target.value, now)
        elif target == RunStatus.PAUSED:
            run.paused_at = now
            run.lease_owner = None
            run.lease_expires_at = None
            await self._end_open_attempt(run_id, "paused", now)
        elif target == RunStatus.RUNNING:
            run.started_at = run.started_at or now
            run.paused_at = None
        await self.db.flush()
        return run

    async def cancel(self, run_id: str, user_id: str) -> bool:
        """用户取消：running → cancelled；非属主或已是终态时幂等返回 False。"""
        run = await self.lock_run(run_id)
        if run.user_id != user_id or RunStatus(run.status) in TERMINAL_RUN_STATUSES:
            return False
        ensure_transition(RunStatus(run.status), RunStatus.CANCELLED)
        now = utc_now()
        run.status = RunStatus.CANCELLED
        run.completed_at = run.completed_at or now
        run.lease_owner = None
        run.lease_expires_at = None
        await self._end_open_attempt(run_id, "cancelled", now)
        await self.db.flush()
        return True

    async def _end_open_attempt(self, run_id: str, outcome: str, now: datetime) -> None:
        open_attempt = await self.db.scalar(
            select(AgentRunAttempt)
            .where(
                AgentRunAttempt.run_id == run_id,
                AgentRunAttempt.ended_at.is_(None),
            )
            .order_by(AgentRunAttempt.attempt.desc())
            .with_for_update()
        )
        if open_attempt is not None:
            open_attempt.ended_at = now
            open_attempt.outcome = outcome

    @staticmethod
    def _owns_active_lease(run: AgentRun, worker_id: str) -> bool:
        return (
            run.lease_owner == worker_id
            and run.lease_expires_at is not None
            and run.lease_expires_at > utc_now()
        )
