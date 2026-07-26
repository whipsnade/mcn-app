from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any
from uuid import uuid4

from app.thinking.contracts import (
    ThinkingBlock,
    ThinkingEvent,
    ThinkingEventType,
    ThinkingOperationSpec,
)
from app.thinking.sanitizer import sanitize_thinking


logger = logging.getLogger(__name__)

_BLOCK_MAX_CHARS = 12_000
_TURN_MAX_CHARS = 30_000
_TRUNCATED_SUFFIX = "思考内容过长，已截断"

ThinkingQueue = asyncio.Queue[ThinkingEvent | None]


@dataclass
class _RunningOperation:
    spec: ThinkingOperationSpec
    attempt: int
    started_at: datetime
    sequence: int = 1
    raw_text: str = ""
    public_text: str = ""
    truncated: bool = False


@dataclass(frozen=True)
class _TurnBinding:
    user_id: str
    session_id: str
    task_id: str | None
    trigger_message_id: str | None


class SessionThinkingSink:
    def __init__(self, service: SessionThinkingService, spec: ThinkingOperationSpec) -> None:
        self._service = service
        self._spec = spec

    async def started(self, *, attempt: int) -> None:
        await self._safe_call(self._service._started, self._spec, attempt)

    async def delta(self, text: str, *, attempt: int) -> None:
        await self._safe_call(self._service._delta, self._spec, text, attempt)

    async def completed(self, *, attempt: int, duration_ms: int) -> None:
        await self._safe_call(
            self._service._terminal,
            self._spec,
            attempt,
            "completed",
            duration_ms,
            None,
        )

    async def failed(self, *, attempt: int, error_code: str) -> None:
        await self._safe_call(
            self._service._terminal,
            self._spec,
            attempt,
            "interrupted",
            None,
            error_code,
        )

    async def _safe_call(self, method: Any, *args: Any) -> None:
        try:
            await method(*args)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("session thinking sink failed", exc_info=True)


class SessionThinkingService:
    def __init__(self, *, queue_size: int = 32) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        self._queue_size = queue_size
        self._lock = asyncio.Lock()
        self._subscribers: dict[str, set[ThinkingQueue]] = {}
        self._running: dict[str, _RunningOperation] = {}
        self._completed: dict[str, list[ThinkingBlock]] = {}
        self._turn_bindings: dict[str, _TurnBinding] = {}

    def create_sink(self, spec: ThinkingOperationSpec) -> SessionThinkingSink:
        return SessionThinkingSink(self, spec)

    async def bind_turn(
        self,
        *,
        turn_id: str,
        user_id: str,
        session_id: str,
        task_id: str | None,
        trigger_message_id: str | None,
    ) -> None:
        async with self._lock:
            existing = self._turn_bindings.get(turn_id)
            if existing is not None and (
                existing.user_id != user_id or existing.session_id != session_id
            ):
                raise ValueError("turn owner does not match")
            binding = _TurnBinding(
                user_id=user_id,
                session_id=session_id,
                task_id=task_id,
                trigger_message_id=trigger_message_id,
            )
            self._turn_bindings[turn_id] = binding
            for state in self._running.values():
                if state.spec.turn_id == turn_id:
                    self._assert_owner(state.spec, user_id, session_id)
                    if state.spec.task_id is None and task_id is not None:
                        state.spec = replace(state.spec, task_id=task_id)
            blocks = self._completed.get(turn_id, [])
            self._completed[turn_id] = [
                replace(block, task_id=task_id)
                if block.task_id is None and task_id is not None
                else block
                for block in blocks
            ]

    async def completed_blocks(
        self,
        *,
        turn_id: str,
        user_id: str,
        session_id: str,
    ) -> tuple[ThinkingBlock, ...]:
        async with self._lock:
            binding = self._turn_bindings.get(turn_id)
            if binding is None:
                return ()
            owner = (binding.user_id, binding.session_id)
            if owner != (user_id, session_id):
                return ()
            return tuple(self._completed.get(turn_id, ()))

    async def subscribe(self, session_id: str) -> ThinkingQueue:
        queue: ThinkingQueue = asyncio.Queue()
        async with self._lock:
            self._subscribers.setdefault(session_id, set()).add(queue)
            states = [
                state for state in self._running.values() if state.spec.session_id == session_id
            ]
            for state in states:
                queue.put_nowait(self._snapshot(state))
        return queue

    async def unsubscribe(self, session_id: str, queue: ThinkingQueue) -> None:
        async with self._lock:
            subscribers = self._subscribers.get(session_id)
            if subscribers is None:
                return
            subscribers.discard(queue)
            if not subscribers:
                self._subscribers.pop(session_id, None)

    async def _started(self, spec: ThinkingOperationSpec, attempt: int) -> None:
        async with self._lock:
            self._register_turn_owner(spec)
            spec = self._bound_spec(spec)
            state = _RunningOperation(
                spec=spec,
                attempt=attempt,
                started_at=datetime.now(UTC),
            )
            self._running[spec.operation_id] = state
            self._publish_locked(
                spec.session_id,
                self._event("thinking.started", self._payload(state)),
                state=state,
            )

    async def _delta(self, spec: ThinkingOperationSpec, text: str, attempt: int) -> None:
        if not text:
            return
        async with self._lock:
            state = self._running.get(spec.operation_id)
            if state is None or state.attempt != attempt:
                return
            previous = state.public_text
            state.raw_text += text
            sanitized = sanitize_thinking(state.raw_text, max_chars=_BLOCK_MAX_CHARS)
            state.public_text = sanitized.text
            state.truncated = sanitized.truncated
            if state.public_text == previous:
                return
            state.sequence += 1
            if state.public_text.startswith(previous):
                event_type: ThinkingEventType = "thinking.delta"
                public_delta = state.public_text[len(previous) :]
                payload = self._payload(state, text=public_delta)
            else:
                event_type = "thinking.snapshot"
                payload = self._payload(state, text=state.public_text)
            self._publish_locked(
                state.spec.session_id,
                self._event(event_type, payload),
                state=state,
            )

    async def _terminal(
        self,
        spec: ThinkingOperationSpec,
        attempt: int,
        status: str,
        duration_ms: int | None,
        error_code: str | None,
    ) -> None:
        async with self._lock:
            state = self._running.get(spec.operation_id)
            if state is None or state.attempt != attempt:
                return
            self._running.pop(spec.operation_id, None)
            state.sequence += 1
            completed_at = datetime.now(UTC)
            elapsed_ms = max(0, int((completed_at - state.started_at).total_seconds() * 1000))
            public_text, turn_truncated = self._fit_turn_budget(
                state.spec.turn_id, state.public_text
            )
            block = ThinkingBlock(
                operation_id=state.spec.operation_id,
                turn_id=state.spec.turn_id,
                purpose=state.spec.purpose,
                attempt=state.attempt,
                label=state.spec.label,
                content=public_text,
                status="completed" if status == "completed" else "interrupted",
                started_at=state.started_at,
                completed_at=completed_at,
                duration_ms=duration_ms if duration_ms is not None else elapsed_ms,
                task_id=state.spec.task_id,
                goal_id=state.spec.goal_id,
                truncated=state.truncated or turn_truncated,
            )
            self._completed.setdefault(state.spec.turn_id, []).append(block)
            event_type: ThinkingEventType = (
                "thinking.completed" if status == "completed" else "thinking.failed"
            )
            payload = self._payload(
                state,
                text=block.content,
                status=block.status,
                duration_ms=block.duration_ms,
                truncated=block.truncated,
            )
            if error_code is not None:
                payload["error_code"] = sanitize_thinking(error_code, max_chars=200).text
            self._publish_locked(
                state.spec.session_id,
                self._event(event_type, payload),
                terminal=True,
            )

    def _register_turn_owner(self, spec: ThinkingOperationSpec) -> None:
        binding = self._turn_bindings.get(spec.turn_id)
        if binding is None:
            self._turn_bindings[spec.turn_id] = _TurnBinding(
                user_id=spec.user_id,
                session_id=spec.session_id,
                task_id=spec.task_id,
                trigger_message_id=None,
            )
            return
        self._assert_owner(spec, binding.user_id, binding.session_id)

    @staticmethod
    def _assert_owner(spec: ThinkingOperationSpec, user_id: str, session_id: str) -> None:
        if spec.user_id != user_id or spec.session_id != session_id:
            raise ValueError("turn owner does not match")

    def _bound_spec(self, spec: ThinkingOperationSpec) -> ThinkingOperationSpec:
        binding = self._turn_bindings[spec.turn_id]
        return replace(spec, task_id=spec.task_id or binding.task_id)

    def _fit_turn_budget(self, turn_id: str, content: str) -> tuple[str, bool]:
        used = sum(len(block.content) for block in self._completed.get(turn_id, ()))
        remaining = max(0, _TURN_MAX_CHARS - used)
        if len(content) <= remaining:
            return content, False
        suffix = _TRUNCATED_SUFFIX[:remaining]
        prefix_length = max(0, remaining - len(suffix))
        return f"{content[:prefix_length]}{suffix}", True

    def _payload(self, state: _RunningOperation, **extra: Any) -> dict[str, Any]:
        binding = self._turn_bindings.get(state.spec.turn_id)
        payload: dict[str, Any] = {
            "operation_id": state.spec.operation_id,
            "turn_id": state.spec.turn_id,
            "session_id": state.spec.session_id,
            "purpose": state.spec.purpose,
            "attempt": state.attempt,
            "sequence": state.sequence,
            "label": state.spec.label,
        }
        optional = {
            "task_id": state.spec.task_id,
            "goal_id": state.spec.goal_id,
            "trigger_message_id": binding.trigger_message_id if binding else None,
        }
        payload.update({key: value for key, value in optional.items() if value is not None})
        payload.update(extra)
        return payload

    @staticmethod
    def _event(event_type: ThinkingEventType, payload: dict[str, Any]) -> ThinkingEvent:
        return ThinkingEvent(id=str(uuid4()), type=event_type, payload=payload)

    def _snapshot(self, state: _RunningOperation) -> ThinkingEvent:
        return self._event(
            "thinking.snapshot",
            self._payload(state, text=state.public_text, truncated=state.truncated),
        )

    def _publish_locked(
        self,
        session_id: str,
        event: ThinkingEvent,
        *,
        state: _RunningOperation | None = None,
        terminal: bool = False,
    ) -> None:
        for queue in tuple(self._subscribers.get(session_id, ())):
            try:
                if queue.qsize() >= self._queue_size:
                    self._clear_queue(queue)
                    for running in self._running.values():
                        if running.spec.session_id == session_id:
                            queue.put_nowait(self._snapshot(running))
                    if terminal or state is None:
                        queue.put_nowait(event)
                else:
                    queue.put_nowait(event)
            except Exception:
                logger.warning("thinking subscriber publish failed", exc_info=True)

    @staticmethod
    def _clear_queue(queue: ThinkingQueue) -> None:
        while not queue.empty():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break


@lru_cache
def get_session_thinking_service() -> SessionThinkingService:
    return SessionThinkingService()


__all__ = [
    "SessionThinkingService",
    "SessionThinkingSink",
    "ThinkingQueue",
    "get_session_thinking_service",
]
