from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.tasks.models import AnalysisTask, TaskEvent
from app.tasks.state import TERMINAL_TASK_STATUSES, TaskEventType, TaskStatus


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class TaskRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_owned(self, task_id: str, user_id: str) -> AnalysisTask:
        task = await self.db.scalar(
            select(AnalysisTask).where(
                AnalysisTask.id == task_id,
                AnalysisTask.user_id == user_id,
            )
        )
        if task is None:
            raise LookupError("task_not_found")
        return task

    async def append_event(
        self,
        task_id: str,
        user_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> TaskEvent:
        event = TaskEvent(
            task_id=task_id,
            user_id=user_id,
            event_type=event_type,
            payload_json=payload,
            created_at=utc_now(),
        )
        self.db.add(event)
        await self.db.flush()
        return event

    async def list_events_after(self, task_id: str, last_event_id: int) -> list[TaskEvent]:
        statement = (
            select(TaskEvent)
            .where(TaskEvent.task_id == task_id, TaskEvent.id > last_event_id)
            .order_by(TaskEvent.id.asc())
        )
        return list((await self.db.scalars(statement)).all())

    async def list_owned_events_after(
        self, task_id: str, user_id: str, last_event_id: int
    ) -> list[TaskEvent]:
        await self.get_owned(task_id, user_id)
        return await self.list_events_after(task_id, last_event_id)

    async def claim_lease(
        self, task_id: str, worker_id: str, lease_seconds: int
    ) -> AnalysisTask | None:
        now = utc_now()
        task = await self.db.scalar(
            select(AnalysisTask).where(AnalysisTask.id == task_id).with_for_update()
        )
        if task is None or task.status in TERMINAL_TASK_STATUSES:
            return None
        if (
            task.lease_owner is not None
            and task.lease_owner != worker_id
            and task.lease_expires_at is not None
            and task.lease_expires_at > now
        ):
            return None
        if task.cancel_requested_at is not None:
            task.status = TaskStatus.CANCELLED
            task.completed_at = now
            task.updated_at = now
            await self.append_event(task.id, task.user_id, TaskEventType.TASK_CANCELLED, {})
            await self.db.flush()
            return None
        task.lease_owner = worker_id
        task.lease_expires_at = now + timedelta(seconds=lease_seconds)
        task.status = TaskStatus.RUNNING if task.plan_json is not None else TaskStatus.PLANNING
        task.started_at = task.started_at or now
        task.updated_at = now
        await self.db.flush()
        return task

    async def save_plan(self, task_id: str, plan_json: dict[str, Any]) -> None:
        task = await self._locked(task_id)
        task.plan_json = plan_json
        task.plan_version = "planner_v1"
        task.status = TaskStatus.RUNNING
        task.updated_at = utc_now()
        await self.append_event(task.id, task.user_id, TaskEventType.PLAN_READY, {"version": "planner_v1"})
        await self.db.flush()

    async def cancel_requested(self, task_id: str) -> bool:
        return bool(
            await self.db.scalar(
                select(AnalysisTask.cancel_requested_at).where(AnalysisTask.id == task_id)
            )
        )

    async def renew_lease(self, task_id: str, worker_id: str, lease_seconds: int) -> None:
        task = await self._locked(task_id)
        if task.lease_owner != worker_id:
            return
        now = utc_now()
        task.lease_expires_at = now + timedelta(seconds=lease_seconds)
        task.updated_at = now
        await self.db.flush()

    async def mark_completed(self, task_id: str, worker_id: str) -> None:
        await self._mark_terminal(task_id, worker_id, TaskStatus.COMPLETED, TaskEventType.TASK_COMPLETED)

    async def mark_cancelled(self, task_id: str, worker_id: str) -> None:
        await self._mark_terminal(task_id, worker_id, TaskStatus.CANCELLED, TaskEventType.TASK_CANCELLED)

    async def mark_interrupted(self, task_id: str, worker_id: str) -> None:
        task = await self._locked(task_id)
        if task.lease_owner == worker_id and task.status not in TERMINAL_TASK_STATUSES:
            task.status = TaskStatus.INTERRUPTED
            task.updated_at = utc_now()
            await self.db.flush()

    async def mark_failed(self, task_id: str, worker_id: str, code: str) -> None:
        task = await self._locked(task_id)
        if task.lease_owner == worker_id and task.status not in TERMINAL_TASK_STATUSES:
            task.status = TaskStatus.FAILED
            task.error_code = code
            task.completed_at = utc_now()
            task.updated_at = task.completed_at
            await self.append_event(task.id, task.user_id, TaskEventType.TASK_FAILED, {"code": code})
            await self.db.flush()

    async def release_lease(self, task_id: str, worker_id: str) -> None:
        task = await self._locked(task_id)
        if task.lease_owner == worker_id:
            task.lease_owner = None
            task.lease_expires_at = None
            task.updated_at = utc_now()
            await self.db.flush()

    async def recoverable_task_ids(self) -> tuple[str, ...]:
        now = utc_now()
        rows = (
            await self.db.scalars(
                select(AnalysisTask.id)
                .where(
                    AnalysisTask.status.in_(
                        (TaskStatus.PLANNING, TaskStatus.RUNNING, TaskStatus.INTERRUPTED)
                    ),
                    (AnalysisTask.lease_expires_at.is_(None))
                    | (AnalysisTask.lease_expires_at <= now),
                )
                .order_by(AnalysisTask.created_at)
            )
        ).all()
        return tuple(rows)

    async def release_expired_unknown(self, task_id: str, observation_seconds: int) -> bool:
        """观察期过后只释放 unknown 预留，绝不重发远端调用。"""
        from app.billing.models import WalletTransaction
        from app.billing.service import WalletService
        from app.mcp_gateway.contracts import McpCallStatus
        from app.mcp_gateway.models import McpCall

        cutoff = utc_now() - timedelta(seconds=observation_seconds)
        calls = (
            await self.db.scalars(
                select(McpCall)
                .where(
                    McpCall.task_id == task_id,
                    McpCall.status == McpCallStatus.UNKNOWN,
                    McpCall.completed_at <= cutoff,
                )
                .with_for_update()
            )
        ).all()
        if not calls:
            return False
        task = await self._locked(task_id)
        wallet = WalletService(self.db)
        for call in calls:
            await wallet.release(
                task.user_id,
                10,
                f"mcp:{call.logical_call_id}:release",
                call.id,
            )
            release_transaction = await self.db.scalar(
                select(WalletTransaction).where(
                    WalletTransaction.idempotency_key == f"mcp:{call.logical_call_id}:release"
                )
            )
            if release_transaction is None:
                raise RuntimeError("mcp_release_ledger_missing")
            call.status = McpCallStatus.RELEASED
            call.settlement_transaction_id = release_transaction.id
            call.updated_at = utc_now()
            await self.append_event(
                task.id,
                task.user_id,
                TaskEventType.POINTS_RELEASED,
                {"logical_call_id": call.logical_call_id, "points": 10},
            )
        task.status = TaskStatus.FAILED
        task.error_code = "mcp_unknown_outcome"
        task.completed_at = utc_now()
        task.updated_at = task.completed_at
        await self.append_event(task.id, task.user_id, TaskEventType.TASK_FAILED, {"code": task.error_code})
        await self.db.flush()
        return True

    async def _locked(self, task_id: str) -> AnalysisTask:
        task = await self.db.scalar(
            select(AnalysisTask).where(AnalysisTask.id == task_id).with_for_update()
        )
        if task is None:
            raise LookupError("task_not_found")
        return task

    async def _mark_terminal(
        self, task_id: str, worker_id: str, status: TaskStatus, event: TaskEventType
    ) -> None:
        task = await self._locked(task_id)
        if task.lease_owner != worker_id or task.status in TERMINAL_TASK_STATUSES:
            return
        now = utc_now()
        task.status = status
        task.completed_at = now
        task.updated_at = now
        await self.append_event(task.id, task.user_id, event, {"status": status})
        await self.db.flush()
