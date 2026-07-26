from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.thinking.contracts import ThinkingBlock
from app.workspace.models import Message, WorkspaceSession


_BRAINSTORM_FAILURE_MESSAGE = "模型暂时无法完成需求理解，请稍后重试。"


class _SessionFactory(Protocol):
    def begin(self) -> AbstractAsyncContextManager[AsyncSession]: ...


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _serialize_block(block: ThinkingBlock) -> dict[str, Any]:
    serialized: dict[str, Any] = {
        "operation_id": block.operation_id,
        "purpose": block.purpose,
        "attempt": block.attempt,
        "label": block.label,
        "content": block.content,
        "status": block.status,
        "started_at": block.started_at.isoformat(),
        "completed_at": block.completed_at.isoformat(),
        "duration_ms": block.duration_ms,
        "truncated": block.truncated,
    }
    if block.task_id is not None:
        serialized["task_id"] = block.task_id
    if block.goal_id is not None:
        serialized["goal_id"] = block.goal_id
    return serialized


def _blocks_from(metadata: dict[str, Any], key: str) -> list[dict[str, Any]]:
    container = metadata.get(key)
    if not isinstance(container, dict):
        return []
    blocks = container.get("blocks")
    if not isinstance(blocks, list):
        return []
    return [dict(block) for block in blocks if isinstance(block, dict)]


def _thinking_metadata(
    blocks: list[dict[str, Any]],
    *,
    force_interrupted: bool = False,
) -> dict[str, Any]:
    deduplicated: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any]] = set()
    for block in blocks:
        key = (block.get("operation_id"), block.get("attempt"))
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(dict(block))
    status = (
        "interrupted"
        if force_interrupted
        or any(block.get("status") == "interrupted" for block in deduplicated)
        else "completed"
    )
    return {"version": 1, "status": status, "blocks": deduplicated}


def _metadata_matches_task(metadata: dict[str, Any], task_id: str) -> bool:
    if metadata.get("task_id") == task_id or metadata.get("latest_analysis_task_id") == task_id:
        return True
    task_ids = metadata.get("analysis_task_ids")
    return isinstance(task_ids, list) and task_id in task_ids


def _partition_blocks_for_task(
    blocks: list[dict[str, Any]],
    task_id: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    matched: list[dict[str, Any]] = []
    remaining: list[dict[str, Any]] = []
    for block in blocks:
        target = matched if block.get("task_id") == task_id else remaining
        target.append(block)
    return matched, remaining


class ThinkingMessageStore:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def persist_block(
        self,
        block: ThinkingBlock,
        *,
        user_id: str,
        session_id: str,
    ) -> None:
        messages = await self._messages(user_id=user_id, session_id=session_id)
        if block.task_id is not None:
            assistant = next(
                (
                    message
                    for message in messages
                    if message.role == "assistant"
                    and _metadata_matches_task(message.metadata_json or {}, block.task_id)
                ),
                None,
            )
        else:
            assistant = next(
                (
                    message
                    for message in messages
                    if message.role == "assistant"
                    and (message.metadata_json or {}).get("turn_id") == block.turn_id
                ),
                None,
            )
        user_message = next(
            (
                message
                for message in messages
                if message.role == "user"
                and (message.metadata_json or {}).get("turn_id") == block.turn_id
            ),
            None,
        )
        if user_message is None and block.task_id is not None:
            user_message = next(
                (
                    message
                    for message in messages
                    if message.role == "user"
                    and _metadata_matches_task(message.metadata_json or {}, block.task_id)
                ),
                None,
            )

        serialized = _serialize_block(block)
        if assistant is None and user_message is None:
            raise LookupError("thinking_turn_message_not_found")

        async with self.db.begin_nested():
            if assistant is not None:
                assistant_metadata = dict(assistant.metadata_json or {})
                pending_blocks: list[dict[str, Any]] = []
                if user_message is not None:
                    user_metadata = dict(user_message.metadata_json or {})
                    pending_blocks, remaining_blocks = _partition_blocks_for_task(
                        _blocks_from(user_metadata, "thinking_pending"),
                        block.task_id,
                    )
                    if remaining_blocks:
                        user_metadata["thinking_pending"] = _thinking_metadata(
                            remaining_blocks
                        )
                    else:
                        user_metadata.pop("thinking_pending", None)
                    user_message.metadata_json = user_metadata
                assistant_metadata.setdefault("turn_id", block.turn_id)
                assistant_metadata["thinking"] = _thinking_metadata(
                    [
                        *_blocks_from(assistant_metadata, "thinking"),
                        *pending_blocks,
                        serialized,
                    ]
                )
                assistant.metadata_json = assistant_metadata
            else:
                assert user_message is not None
                user_metadata = dict(user_message.metadata_json or {})
                user_metadata["thinking_pending"] = _thinking_metadata(
                    [*_blocks_from(user_metadata, "thinking_pending"), serialized]
                )
                user_message.metadata_json = user_metadata
            await self.db.flush()

    async def attach_turn_to_assistant(
        self,
        message,
        *,
        user_id: str,
        session_id: str,
        turn_id: str,
    ) -> None:
        if (
            message.user_id != user_id
            or message.session_id != session_id
            or message.role != "assistant"
        ):
            raise LookupError("assistant_message_not_owned")

        messages = await self._messages(user_id=user_id, session_id=session_id)
        user_message = next(
            (
                candidate
                for candidate in messages
                if candidate.role == "user"
                and (candidate.metadata_json or {}).get("turn_id") == turn_id
            ),
            None,
        )
        assistant_metadata = dict(message.metadata_json or {})
        assistant_metadata["turn_id"] = turn_id
        target_task_id = assistant_metadata.get("task_id")
        if not isinstance(target_task_id, str):
            target_task_id = None
        async with self.db.begin_nested():
            if user_message is not None:
                user_metadata = dict(user_message.metadata_json or {})
                pending_blocks, remaining_blocks = _partition_blocks_for_task(
                    _blocks_from(user_metadata, "thinking_pending"),
                    target_task_id,
                )
                if pending_blocks:
                    assistant_metadata["thinking"] = _thinking_metadata(
                        [
                            *_blocks_from(assistant_metadata, "thinking"),
                            *pending_blocks,
                        ]
                    )
                if remaining_blocks:
                    user_metadata["thinking_pending"] = _thinking_metadata(
                        remaining_blocks
                    )
                else:
                    user_metadata.pop("thinking_pending", None)
                user_message.metadata_json = user_metadata
            message.metadata_json = assistant_metadata
            await self.db.flush()

    async def _messages(self, *, user_id: str, session_id: str) -> list[Message]:
        statement = (
            select(Message)
            .where(
                Message.user_id == user_id,
                Message.session_id == session_id,
            )
            .order_by(Message.sequence)
            .with_for_update()
        )
        return list((await self.db.scalars(statement)).all())


async def record_brainstorm_failure(
    session_factory: _SessionFactory,
    *,
    user_id: str,
    session_id: str,
    turn_id: str,
    user_content: str,
    blocks: tuple[ThinkingBlock, ...],
    error_code: str,
) -> Message:
    async with session_factory.begin() as db:
        workspace = await db.scalar(
            select(WorkspaceSession)
            .where(
                WorkspaceSession.id == session_id,
                WorkspaceSession.user_id == user_id,
                WorkspaceSession.deleted_at.is_(None),
            )
            .with_for_update()
        )
        if workspace is None:
            raise LookupError("session_not_found")

        store = ThinkingMessageStore(db)
        messages = await store._messages(user_id=user_id, session_id=session_id)
        user_message = next(
            (
                message
                for message in messages
                if message.role == "user"
                and (message.metadata_json or {}).get("turn_id") == turn_id
            ),
            None,
        )
        if user_message is None:
            sequence = (
                await db.scalar(
                    select(func.max(Message.sequence)).where(
                        Message.user_id == user_id,
                        Message.session_id == session_id,
                    )
                )
                or 0
            ) + 1
            user_message = Message(
                id=str(uuid4()),
                session_id=session_id,
                user_id=user_id,
                role="user",
                content=user_content,
                sequence=sequence,
                metadata_json={"turn_id": turn_id},
                created_at=_utc_now(),
            )
            db.add(user_message)
            await db.flush()
            messages.append(user_message)
        else:
            user_metadata = dict(user_message.metadata_json or {})
            user_metadata.setdefault("turn_id", turn_id)
            user_message.metadata_json = user_metadata

        existing = next(
            (
                message
                for message in messages
                if message.role == "assistant"
                and (message.metadata_json or {}).get("turn_id") == turn_id
                and (message.metadata_json or {}).get("message_type") == "error"
            ),
            None,
        )
        serialized_blocks = [
            _serialize_block(block) for block in blocks if block.turn_id == turn_id
        ]
        if existing is not None:
            existing_metadata = dict(existing.metadata_json or {})
            existing_metadata["thinking"] = _thinking_metadata(
                [
                    *_blocks_from(existing_metadata, "thinking"),
                    *serialized_blocks,
                ],
                force_interrupted=True,
            )
            existing.metadata_json = existing_metadata
            await db.flush()
            return existing

        sequence = (
            await db.scalar(
                select(func.max(Message.sequence)).where(
                    Message.user_id == user_id,
                    Message.session_id == session_id,
                )
            )
            or 0
        ) + 1
        assistant = Message(
            id=str(uuid4()),
            session_id=session_id,
            user_id=user_id,
            role="assistant",
            content=_BRAINSTORM_FAILURE_MESSAGE,
            sequence=sequence,
            metadata_json={
                "turn_id": turn_id,
                "message_type": "error",
                "error_code": error_code,
                "thinking": _thinking_metadata(
                    serialized_blocks,
                    force_interrupted=True,
                ),
            },
            created_at=_utc_now(),
        )
        db.add(assistant)
        user_metadata = dict(user_message.metadata_json or {})
        user_metadata.pop("thinking_pending", None)
        user_message.metadata_json = user_metadata
        await db.flush()
        return assistant
