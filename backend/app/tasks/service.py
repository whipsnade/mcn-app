from datetime import UTC, datetime
import hashlib
import json
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.tasks.models import AnalysisTask
from app.tasks.repository import TaskRepository
from app.tasks.schemas import TaskCreate
from app.tasks.state import TERMINAL_TASK_STATUSES, TaskEventType, TaskStatus
from app.workspace.schemas import MessageCreate
from app.workspace.models import Message
from app.workspace.service import WorkspaceService


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class TaskConflictError(RuntimeError):
    pass


def idempotency_key_digest(key: str) -> str:
    """Hash a normalized key so the raw client token is never persisted/logged."""
    normalized = key.strip()
    if not normalized or len(normalized) > 128:
        raise ValueError("invalid_idempotency_key")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def idempotency_payload_digest(content: str, scoring_profile: str) -> str:
    normalized = {
        "content": content.strip(),
        "scoring_profile": scoring_profile,
    }
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def can_retry_status(status: str | TaskStatus) -> bool:
    try:
        return TaskStatus(status) in TERMINAL_TASK_STATUSES
    except ValueError:
        return False


class TaskService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repository = TaskRepository(db)

    async def create(
        self,
        user_id: str,
        session_id: str,
        payload: TaskCreate,
        *,
        trigger_message_id: str | None = None,
        retry_of_task_id: str | None = None,
        retry_key: str | None = None,
        idempotency_key_hash: str | None = None,
        idempotency_payload_hash: str | None = None,
    ) -> AnalysisTask:
        workspace_service = WorkspaceService(self.db)
        await workspace_service.get_owned_session(user_id, session_id, for_update=True)
        active_task_id = await self.db.scalar(
            select(AnalysisTask.id)
            .where(
                AnalysisTask.session_id == session_id,
                AnalysisTask.status.notin_(
                    tuple(item.value for item in TERMINAL_TASK_STATUSES)
                ),
            )
            .limit(1)
        )
        if active_task_id is not None:
            raise TaskConflictError("task_in_progress")
        if trigger_message_id is None:
            message = await workspace_service.append_message(
                user_id, session_id, MessageCreate(content=payload.content)
            )
        else:
            message = await self.db.scalar(
                select(Message).where(
                    Message.id == trigger_message_id,
                    Message.session_id == session_id,
                    Message.user_id == user_id,
                )
            )
            if message is None:
                raise LookupError("trigger_message_not_found")
        message.metadata_json = {"scoring_profile": payload.scoring_profile}
        now = utc_now()
        latest_creation_order = await self.db.scalar(
            select(func.max(AnalysisTask.creation_order)).where(
                AnalysisTask.session_id == session_id
            )
        )
        task = AnalysisTask(
            id=str(uuid4()),
            user_id=user_id,
            session_id=session_id,
            trigger_message_id=message.id,
            retry_of_task_id=retry_of_task_id,
            retry_key=retry_key,
            idempotency_key_hash=idempotency_key_hash,
            idempotency_payload_hash=idempotency_payload_hash,
            status=TaskStatus.PENDING,
            plan_json=None,
            plan_version=None,
            max_calls=10,
            estimated_points=0,
            error_code=None,
            error_message=None,
            cancel_requested_at=None,
            lease_owner=None,
            lease_expires_at=None,
            started_at=None,
            completed_at=None,
            creation_order=int(latest_creation_order or 0) + 1,
            created_at=now,
            updated_at=now,
        )
        message.metadata_json = {
            **message.metadata_json,
            "analysis_task_ids": [*(message.metadata_json.get("analysis_task_ids", [])), task.id],
            "latest_analysis_task_id": task.id,
        }
        self.db.add(task)
        await self.db.flush()
        await self.repository.append_event(
            task.id,
            task.user_id,
            TaskEventType.TASK_PENDING,
            {"status": TaskStatus.PENDING, "phase": "accepting_data", "label": "接受数据"},
        )
        return task

    async def create_idempotent(
        self,
        user_id: str,
        session_id: str,
        payload: TaskCreate,
        idempotency_key: str,
    ) -> tuple[AnalysisTask, bool]:
        """Create once per user/session/key, atomically across processes."""
        key_hash = idempotency_key_digest(idempotency_key)
        payload_hash = idempotency_payload_digest(payload.content, payload.scoring_profile)
        workspace_service = WorkspaceService(self.db)
        await workspace_service.get_owned_session(user_id, session_id, for_update=True)
        existing = await self.db.scalar(
            select(AnalysisTask).where(
                AnalysisTask.user_id == user_id,
                AnalysisTask.session_id == session_id,
                AnalysisTask.idempotency_key_hash == key_hash,
            )
        )
        if existing is not None:
            if existing.idempotency_payload_hash != payload_hash:
                raise TaskConflictError("idempotency_payload_mismatch")
            return existing, True
        try:
            async with self.db.begin_nested():
                task = await self.create(
                    user_id,
                    session_id,
                    payload,
                    idempotency_key_hash=key_hash,
                    idempotency_payload_hash=payload_hash,
                )
        except IntegrityError:
            # Another process won the unique index race. The savepoint keeps
            # its message/task changes intact and lets us read the winner.
            existing = await self.db.scalar(
                select(AnalysisTask).where(
                    AnalysisTask.user_id == user_id,
                    AnalysisTask.session_id == session_id,
                    AnalysisTask.idempotency_key_hash == key_hash,
                )
            )
            if existing is None:
                raise
            if existing.idempotency_payload_hash != payload_hash:
                raise TaskConflictError("idempotency_payload_mismatch")
            return existing, True
        return task, False

    async def retry(self, user_id: str, task_id: str) -> AnalysisTask:
        source = await self.repository.get_owned(task_id, user_id)
        if not can_retry_status(source.status):
            raise TaskConflictError("task_not_retryable")
        await WorkspaceService(self.db).get_owned_session(user_id, source.session_id, for_update=True)
        active_session_task = await self.db.scalar(
            select(AnalysisTask).where(
                AnalysisTask.session_id == source.session_id,
                AnalysisTask.status.notin_(tuple(item.value for item in TERMINAL_TASK_STATUSES)),
            ).limit(1)
        )
        if active_session_task is not None:
            if active_session_task.retry_of_task_id == source.id:
                return active_session_task
            raise TaskConflictError("task_in_progress")
        message = await self.db.scalar(
            select(Message).where(
                Message.id == source.trigger_message_id,
                Message.session_id == source.session_id,
                Message.user_id == user_id,
            )
        )
        if message is None:
            raise LookupError("trigger_message_not_found")
        retry_key = f"retry:{source.id}"
        active = await self.db.scalar(
            select(AnalysisTask).where(AnalysisTask.retry_key == retry_key)
        )
        if active is not None and not can_retry_status(active.status):
            return active
        try:
            return await self.create(
                user_id,
                source.session_id,
                TaskCreate(content=message.content),
                trigger_message_id=message.id,
                retry_of_task_id=source.id,
                retry_key=retry_key,
            )
        except IntegrityError:
            existing = await self.db.scalar(
                select(AnalysisTask).where(AnalysisTask.retry_key == retry_key)
            )
            if existing is None:
                raise
            return existing

    async def cancel(self, user_id: str, task_id: str) -> AnalysisTask:
        task = await self.repository.get_owned(task_id, user_id)
        if task.cancel_requested_at is None:
            task.cancel_requested_at = utc_now()
            task.updated_at = task.cancel_requested_at
            await self.db.flush()
        return task
