from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.tasks.models import AnalysisTask, TaskEvent
from app.tasks.errors import SafeTaskError, safe_error
from app.tasks.state import TERMINAL_TASK_STATUSES, TaskEventType, TaskStatus
from app.workspace.models import Message, WorkspaceSession


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class TaskRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_owned(self, task_id: str, user_id: str) -> AnalysisTask:
        task = await self.db.scalar(
            select(AnalysisTask)
            .join(WorkspaceSession, WorkspaceSession.id == AnalysisTask.session_id)
            .where(
                AnalysisTask.id == task_id,
                AnalysisTask.user_id == user_id,
                WorkspaceSession.deleted_at.is_(None),
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
        if task.lease_owner is not None and (
            task.lease_expires_at is None or task.lease_expires_at > now
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

    async def save_plan(self, task_id: str, worker_id: str, plan_json: dict[str, Any]) -> bool:
        task = await self._locked(task_id)
        if not self._owns_active_lease(task, worker_id):
            return False
        task.plan_json = plan_json
        task.plan_version = "agent_trajectory_v1"
        task.status = TaskStatus.RUNNING
        task.updated_at = utc_now()
        await self.append_event(
            task.id,
            task.user_id,
            TaskEventType.PLAN_READY,
            {"version": "agent_trajectory_v1", "phase": "ai_analysis", "label": "AI 分析"},
        )
        await self.db.flush()
        return True

    async def save_trajectory(
        self, task_id: str, worker_id: str, trajectory_json: dict[str, Any]
    ) -> bool:
        """Persist the agent loop trajectory without re-emitting plan.ready."""
        task = await self._locked(task_id)
        if not self._owns_active_lease(task, worker_id):
            return False
        task.plan_json = trajectory_json
        task.plan_version = "agent_trajectory_v1"
        task.status = TaskStatus.RUNNING
        task.updated_at = utc_now()
        await self.db.flush()
        return True

    async def cancel_requested(self, task_id: str) -> bool:
        return bool(
            await self.db.scalar(
                select(AnalysisTask.cancel_requested_at).where(AnalysisTask.id == task_id)
            )
        )

    async def renew_lease(self, task_id: str, worker_id: str, lease_seconds: int) -> bool:
        task = await self._locked(task_id)
        if not self._owns_active_lease(task, worker_id):
            return False
        now = utc_now()
        task.lease_expires_at = now + timedelta(seconds=lease_seconds)
        task.updated_at = now
        await self.db.flush()
        return True

    async def mark_completed(self, task_id: str, worker_id: str) -> bool:
        return await self._mark_terminal(
            task_id,
            worker_id,
            TaskStatus.COMPLETED,
            TaskEventType.TASK_COMPLETED,
        )

    async def mark_completed_with_warnings(
        self, task_id: str, worker_id: str, warning_code: str, warning_message: str | None = None
    ) -> bool:
        task = await self._locked(task_id)
        if not self._owns_active_lease(task, worker_id) or task.status in TERMINAL_TASK_STATUSES:
            return False
        now = utc_now()
        task.status = TaskStatus.COMPLETED_WITH_WARNINGS
        task.retry_key = None
        failure = safe_error(warning_code, warning_message)
        task.error_code = failure.code
        task.error_message = failure.message
        task.completed_at = now
        task.updated_at = now
        message = await self._append_error_message(task, failure)
        await self.append_event(
            task.id,
            task.user_id,
            TaskEventType.TASK_COMPLETED_WITH_WARNINGS,
            {"code": failure.code, "message": failure.message, "message_id": message.id},
        )
        await self.db.flush()
        return True

    async def mark_cancelled(self, task_id: str, worker_id: str) -> bool:
        return await self._mark_terminal(
            task_id,
            worker_id,
            TaskStatus.CANCELLED,
            TaskEventType.TASK_CANCELLED,
        )

    async def mark_interrupted(self, task_id: str, worker_id: str) -> bool:
        task = await self._locked(task_id)
        if not self._owns_active_lease(task, worker_id) or task.status in TERMINAL_TASK_STATUSES:
            return False
        task.status = TaskStatus.INTERRUPTED
        task.updated_at = utc_now()
        await self.db.flush()
        return True

    async def mark_failed(
        self, task_id: str, worker_id: str, code: str, message: str | None = None
    ) -> bool:
        task = await self._locked(task_id)
        if not self._owns_active_lease(task, worker_id) or task.status in TERMINAL_TASK_STATUSES:
            return False
        failure = safe_error(code, message)
        task.status = TaskStatus.FAILED
        task.retry_key = None
        task.error_code = failure.code
        task.error_message = failure.message
        task.completed_at = utc_now()
        task.updated_at = task.completed_at
        error_message = await self._append_error_message(task, failure)
        await self.append_event(
            task.id,
            task.user_id,
            TaskEventType.TASK_FAILED,
            {"code": failure.code, "message": failure.message, "message_id": error_message.id},
        )
        await self.db.flush()
        return True

    async def mark_insufficient_balance(self, task_id: str, worker_id: str) -> bool:
        """余额不足终态：与 mark_failed 同构，状态为 INSUFFICIENT_BALANCE。"""
        task = await self._locked(task_id)
        if not self._owns_active_lease(task, worker_id) or task.status in TERMINAL_TASK_STATUSES:
            return False
        failure = safe_error("insufficient_balance")
        task.status = TaskStatus.INSUFFICIENT_BALANCE
        task.retry_key = None
        task.error_code = failure.code
        task.error_message = failure.message
        task.completed_at = utc_now()
        task.updated_at = task.completed_at
        error_message = await self._append_error_message(task, failure)
        await self.append_event(
            task.id,
            task.user_id,
            TaskEventType.TASK_FAILED,
            {"code": failure.code, "message": failure.message, "message_id": error_message.id},
        )
        await self.db.flush()
        return True

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

    async def pending_followup_task_ids(self) -> tuple[str, ...]:
        """Find completed rounds whose post-processing model call was interrupted."""
        tasks = list(
            (
                await self.db.scalars(
                    select(AnalysisTask)
                    .join(WorkspaceSession, WorkspaceSession.id == AnalysisTask.session_id)
                    .where(
                        WorkspaceSession.deleted_at.is_(None),
                        AnalysisTask.status.in_(
                            (TaskStatus.COMPLETED, TaskStatus.COMPLETED_WITH_WARNINGS)
                        ),
                    )
                )
            ).all()
        )
        result: list[str] = []
        for task in tasks:
            messages = list(
                (
                    await self.db.scalars(
                        select(Message).where(
                            Message.session_id == task.session_id,
                            Message.user_id == task.user_id,
                            Message.role == "assistant",
                        )
                    )
                ).all()
            )
            if any(
                message.metadata_json.get("task_id") == task.id
                and message.metadata_json.get("followup_suggestions_status") != "completed"
                and (
                    message.metadata_json.get("followup_suggestions_status") in {None, "pending"}
                    or (
                        message.metadata_json.get("followup_suggestions_status") == "failed"
                        and int(message.metadata_json.get("followup_attempts", 0)) < 3
                    )
                )
                for message in messages
            ):
                result.append(task.id)
        return tuple(result)

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
        failure = safe_error("mcp_unknown_outcome")
        task.status = TaskStatus.FAILED
        task.retry_key = None
        task.error_code = failure.code
        task.error_message = failure.message
        task.completed_at = utc_now()
        task.updated_at = task.completed_at
        error_message = await self._append_error_message(task, failure)
        await self.append_event(
            task.id,
            task.user_id,
            TaskEventType.TASK_FAILED,
            {"code": failure.code, "message": failure.message, "message_id": error_message.id},
        )
        await self.db.flush()
        return True

    async def _locked(self, task_id: str) -> AnalysisTask:
        task = await self.db.scalar(
            select(AnalysisTask).where(AnalysisTask.id == task_id).with_for_update()
        )
        if task is None:
            raise LookupError("task_not_found")
        return task

    async def _append_error_message(self, task: AnalysisTask, failure: SafeTaskError) -> Message:
        key = f"{task.id}:{failure.code}"
        existing = await self.db.scalar(
            select(Message).where(Message.error_idempotency_key == key)
        )
        if existing is not None:
            return existing
        sequence = (
            await self.db.scalar(
                select(func.max(Message.sequence)).where(Message.session_id == task.session_id)
            )
            or 0
        ) + 1
        message = Message(
            id=str(uuid4()),
            session_id=task.session_id,
            user_id=task.user_id,
            role="assistant",
            content=failure.message,
            sequence=sequence,
            metadata_json={"task_id": task.id, "message_type": "error", "error_code": failure.code},
            error_idempotency_key=key,
            created_at=utc_now(),
        )
        try:
            async with self.db.begin_nested():
                self.db.add(message)
                await self.db.flush()
        except IntegrityError:
            existing = await self.db.scalar(
                select(Message).where(Message.error_idempotency_key == key)
            )
            if existing is None:
                raise
            return existing
        return message

    async def _mark_terminal(
        self, task_id: str, worker_id: str, status: TaskStatus, event: TaskEventType
    ) -> bool:
        task = await self._locked(task_id)
        if not self._owns_active_lease(task, worker_id) or task.status in TERMINAL_TASK_STATUSES:
            return False
        now = utc_now()
        task.status = status
        task.retry_key = None
        task.completed_at = now
        task.updated_at = now
        await self.append_event(task.id, task.user_id, event, {"status": status})
        await self.db.flush()
        return True

    @staticmethod
    def _owns_active_lease(task: AnalysisTask, worker_id: str) -> bool:
        return (
            task.lease_owner == worker_id
            and task.lease_expires_at is not None
            and task.lease_expires_at > utc_now()
        )
