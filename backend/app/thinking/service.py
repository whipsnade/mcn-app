from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict, deque
from collections.abc import Iterable
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
_FOLDED_PLACEHOLDER = "「早期思考已折叠」"
# 块已截断（滑动窗口）后，距上次发布的 raw 增长达到该阈值才发布新 snapshot，控制带宽。
_TRUNCATED_SNAPSHOT_MIN_RAW_GROWTH = 1_000
# 每会话保留的近期终态事件条数：断线重连后回放，避免前端永久停留在「思考中」。
_RECENT_TERMINAL_LIMIT = 200
# 进程内最多保留的 turn 状态数（完成块、绑定、持久化游标）；超限按最旧淘汰。
_DEFAULT_MAX_RETAINED_TURNS = 256

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
    last_published_raw_len: int = 0


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
    def __init__(
        self,
        *,
        queue_size: int = 32,
        max_retained_turns: int = _DEFAULT_MAX_RETAINED_TURNS,
    ) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        if max_retained_turns < 1:
            raise ValueError("max_retained_turns must be positive")
        self._queue_size = queue_size
        self._max_retained_turns = max_retained_turns
        self._lock = asyncio.Lock()
        self._subscribers: dict[str, set[ThinkingQueue]] = {}
        self._running: dict[str, _RunningOperation] = {}
        self._completed: dict[str, list[ThinkingBlock]] = {}
        self._turn_bindings: dict[str, _TurnBinding] = {}
        self._persisted: dict[str, set[tuple[str, int]]] = {}
        self._turn_order: OrderedDict[str, None] = OrderedDict()
        self._recent_terminal: dict[str, deque[ThinkingEvent]] = {}

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
            self._touch_turn_locked(turn_id)
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
            self._evict_turns_locked()

    async def completed_blocks(
        self,
        *,
        turn_id: str,
        user_id: str,
        session_id: str,
        only_unpersisted: bool = False,
    ) -> tuple[ThinkingBlock, ...]:
        async with self._lock:
            binding = self._turn_bindings.get(turn_id)
            if binding is None:
                return ()
            owner = (binding.user_id, binding.session_id)
            if owner != (user_id, session_id):
                return ()
            blocks = self._completed.get(turn_id, ())
            if only_unpersisted:
                persisted = self._persisted.get(turn_id, set())
                blocks = [
                    block
                    for block in blocks
                    if (block.operation_id, block.attempt) not in persisted
                ]
            return tuple(blocks)

    async def mark_blocks_persisted(
        self,
        *,
        turn_id: str,
        user_id: str,
        session_id: str,
        keys: Iterable[tuple[str, int]],
    ) -> None:
        """登记已成功落库的 (operation_id, attempt)，避免重复扫描与重复持久化。"""
        async with self._lock:
            binding = self._turn_bindings.get(turn_id)
            if binding is None:
                return
            if (binding.user_id, binding.session_id) != (user_id, session_id):
                return
            self._persisted.setdefault(turn_id, set()).update(keys)

    async def subscribe(self, session_id: str) -> ThinkingQueue:
        queue: ThinkingQueue = asyncio.Queue()
        async with self._lock:
            self._subscribers.setdefault(session_id, set()).add(queue)
            states = [
                state for state in self._running.values() if state.spec.session_id == session_id
            ]
            for state in states:
                queue.put_nowait(self._snapshot(state))
            # 回放近期终态事件：断线期间完成的 operation 也能收敛到终态。
            # 前端按 operation+attempt+sequence 去重，重复回放不会产生副作用。
            for event in self._recent_terminal.get(session_id, ()):
                queue.put_nowait(event)
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
            self._touch_turn_locked(spec.turn_id)
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
            self._evict_turns_locked()

    async def _delta(self, spec: ThinkingOperationSpec, text: str, attempt: int) -> None:
        if not text:
            return
        async with self._lock:
            state = self._running.get(spec.operation_id)
            if state is None or state.attempt != attempt:
                return
            previous = state.public_text
            state.raw_text += text
            # turn 级预算实时收敛：已完成的同 turn block 占用额度后，
            # 运行中的 delta 立即截断，不再等到终态才发现超限。
            used = sum(
                len(block.content)
                for block in self._completed.get(state.spec.turn_id, ())
            )
            remaining = max(0, _TURN_MAX_CHARS - used)
            sanitized = sanitize_thinking(
                state.raw_text, max_chars=min(_BLOCK_MAX_CHARS, remaining)
            )
            state.public_text = sanitized.text
            state.truncated = sanitized.truncated
            if state.public_text == previous:
                return
            if state.truncated:
                # 滑动窗口下内容不再前缀递增，只能整段替换；raw 增长不足阈值时
                # 仅更新状态不发布事件（节流），客户端看到略旧内容属有意取舍。
                if (
                    len(state.raw_text) - state.last_published_raw_len
                    < _TRUNCATED_SNAPSHOT_MIN_RAW_GROWTH
                ):
                    return
                event_type: ThinkingEventType = "thinking.snapshot"
            elif state.public_text.startswith(previous):
                event_type = "thinking.delta"
            else:
                event_type = "thinking.snapshot"
            state.sequence += 1
            state.last_published_raw_len = len(state.raw_text)
            if event_type == "thinking.delta":
                public_delta = state.public_text[len(previous) :]
                payload = self._payload(state, text=public_delta)
            else:
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
            public_text, truncated = self._fit_turn_budget(
                state.spec.turn_id, state.raw_text
            )
            if not public_text and truncated:
                # 防御：折叠后预算必足（单块 12k < turn 30k）；仍避免落库空串。
                public_text = _FOLDED_PLACEHOLDER
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
                truncated=truncated,
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
            event = self._event(event_type, payload)
            self._record_recent_terminal_locked(state.spec.session_id, event)
            self._touch_turn_locked(state.spec.turn_id)
            self._publish_locked(
                state.spec.session_id,
                event,
                terminal=True,
            )
            self._evict_turns_locked()

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

    def _touch_turn_locked(self, turn_id: str) -> None:
        self._turn_order[turn_id] = None
        self._turn_order.move_to_end(turn_id)

    def _evict_turns_locked(self) -> None:
        """按最旧顺序释放 turn 状态；仍有运行中 operation 的 turn 跳过不淘汰。"""
        overflow = len(self._turn_order) - self._max_retained_turns
        if overflow <= 0:
            return
        active_turns = {state.spec.turn_id for state in self._running.values()}
        for turn_id in list(self._turn_order):
            if overflow <= 0:
                break
            if turn_id in active_turns:
                continue
            self._turn_order.pop(turn_id, None)
            self._turn_bindings.pop(turn_id, None)
            self._completed.pop(turn_id, None)
            self._persisted.pop(turn_id, None)
            overflow -= 1

    def _record_recent_terminal_locked(self, session_id: str, event: ThinkingEvent) -> None:
        recent = self._recent_terminal.get(session_id)
        if recent is None:
            recent = deque(maxlen=_RECENT_TERMINAL_LIMIT)
            self._recent_terminal[session_id] = recent
            # 会话数同样有界：超出的最旧会话整体丢弃。
            if len(self._recent_terminal) > self._max_retained_turns * 2:
                self._recent_terminal.pop(next(iter(self._recent_terminal)))
        recent.append(event)

    @staticmethod
    def _assert_owner(spec: ThinkingOperationSpec, user_id: str, session_id: str) -> None:
        if spec.user_id != user_id or spec.session_id != session_id:
            raise ValueError("turn owner does not match")

    def _bound_spec(self, spec: ThinkingOperationSpec) -> ThinkingOperationSpec:
        binding = self._turn_bindings[spec.turn_id]
        return replace(spec, task_id=spec.task_id or binding.task_id)

    def _fit_turn_budget(self, turn_id: str, raw_text: str) -> tuple[str, bool]:
        """按 turn 预算收敛新块内容：额度不足时按完成顺序折叠最旧已完成块。

        折叠只改 content（占位符 + truncated=True），不改 (operation_id, attempt)，
        持久化游标不受影响；已是占位符的块跳过，保证幂等。不向在线客户端补发
        折叠事件：live 客户端已收全文，刷新后以落库折叠态为准。
        """
        blocks = self._completed.get(turn_id, [])
        while True:
            used = sum(len(block.content) for block in blocks)
            remaining = max(0, _TURN_MAX_CHARS - used)
            budget = min(_BLOCK_MAX_CHARS, remaining)
            sanitized = sanitize_thinking(raw_text, max_chars=budget)
            if not sanitized.truncated or remaining >= _BLOCK_MAX_CHARS:
                # 未截断，或截断是单块上限所致（折叠无法释放更多额度）。
                return sanitized.text, sanitized.truncated
            target = next(
                (
                    index
                    for index, block in enumerate(blocks)
                    if block.content != _FOLDED_PLACEHOLDER
                ),
                None,
            )
            if target is None:
                return sanitized.text, True
            blocks[target] = replace(
                blocks[target], content=_FOLDED_PLACEHOLDER, truncated=True
            )

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
