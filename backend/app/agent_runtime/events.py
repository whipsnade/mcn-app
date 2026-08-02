"""统一 Agent Run 的持久事件流（spec §15.3）。

事件持久化到 ``agent_events``（``run_id`` + per-run ``sequence`` 唯一），
通过进程内 ``AgentEventBroker`` 唤醒订阅方；数据库是唯一事实来源，
broker 只承担"有新事件"的唤醒信号。

事件 payload 约定（spec §15.3）：
- 所有事件 payload 必须带 ``run_id``（服务端强制写入，客户端不可伪造）；
- Artifact 事件带 ``artifact_id/module/parent_artifact_id/status``；
- Review 事件带 ``review_batch_id/artifact_id/draft_revision_id``。
``append`` 会自动把 ``run_id`` 合入 payload，调用方无需重复携带。
"""

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.models import AgentEvent, AgentRun


class AgentEventType(StrEnum):
    RUN_STARTED = "run.started"
    RUN_PAUSED = "run.paused"
    RUN_RESUMED = "run.resumed"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"
    THINKING_STARTED = "thinking.started"
    THINKING_DELTA = "thinking.delta"
    THINKING_COMPLETED = "thinking.completed"
    THINKING_FAILED = "thinking.failed"
    TOOL_STARTED = "tool.started"
    TOOL_SUCCEEDED = "tool.succeeded"
    TOOL_FAILED = "tool.failed"
    TOOL_UNKNOWN = "tool.unknown"
    ARTIFACT_DRAFT_CREATED = "artifact.draft.created"
    ARTIFACT_DRAFT_UPDATED = "artifact.draft.updated"
    REVIEW_STARTED = "review.started"
    REVIEW_REVISION_REQUESTED = "review.revision_requested"
    REVIEW_APPROVED = "review.approved"
    REVIEW_REJECTED = "review.rejected"
    ARTIFACT_PUBLISHED = "artifact.published"
    MESSAGE_COMPLETED = "message.completed"


# 终态事件：送达后流必须结束，前端据此判定 Run 已出结果。
TERMINAL_EVENT_TYPES = frozenset(
    {
        AgentEventType.RUN_COMPLETED,
        AgentEventType.RUN_FAILED,
        AgentEventType.RUN_CANCELLED,
    }
)


def is_terminal_event(event_type: str) -> bool:
    return event_type in TERMINAL_EVENT_TYPES


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class RunEventForbidden(Exception):
    """Run 事件流归属校验失败：Run 不存在或不属于请求用户（映射 404）。"""


class AgentEventBroker:
    """按 ``run_id`` 的进程内订阅注册表；仅作唤醒信号，不作存储。"""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[AgentEvent]]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def subscribe(self, run_id: str) -> asyncio.Queue[AgentEvent]:
        queue: asyncio.Queue[AgentEvent] = asyncio.Queue()
        async with self._lock:
            self._subscribers[run_id].add(queue)
        return queue

    async def unsubscribe(self, run_id: str, queue: asyncio.Queue[AgentEvent]) -> None:
        async with self._lock:
            subscribers = self._subscribers.get(run_id)
            if subscribers is None:
                return
            subscribers.discard(queue)
            if not subscribers:
                del self._subscribers[run_id]

    async def publish(self, event: AgentEvent) -> None:
        async with self._lock:
            subscribers = tuple(self._subscribers.get(event.run_id, ()))
        for queue in subscribers:
            await queue.put(event)


class AgentEventStream:
    """持久事件流的写入与读取。``db`` 是共享的 AsyncSession（SSE 请求级）。"""

    def __init__(self, db: AsyncSession, broker: AgentEventBroker) -> None:
        self.db = db
        self.broker = broker

    async def append(
        self, run_id: str, user_id: str, event_type: str, payload: dict[str, Any] | None
    ) -> AgentEvent:
        """分配下一条 per-run sequence 并持久化，成功后广播到 broker。

        以 ``SELECT ... FOR UPDATE`` 锁住 Run 行序列化并发 append，
        +``(run_id, sequence)`` 唯一约束兜底，保证 sequence 连续无空洞、无重复。

        append 是事件行的提交点：先 ``commit()`` 再 ``publish()``，回滚窗口内
        不会产生幽灵 broker 事件。调用方不应把 append 放进打算回滚的更大事务里。
        归属校验拒绝非属主注入（含伪造终态事件），与 stream 一致。
        """
        run = await self.db.scalar(
            select(AgentRun).where(AgentRun.id == run_id).with_for_update()
        )
        if run is None:
            raise RunEventForbidden("run_not_found")
        if run.user_id != user_id:
            raise RunEventForbidden("run_not_owned")
        max_sequence = await self.db.scalar(
            select(func.max(AgentEvent.sequence)).where(AgentEvent.run_id == run_id)
        )
        event = AgentEvent(
            id=str(uuid4()),
            run_id=run_id,
            user_id=user_id,
            sequence=(max_sequence or 0) + 1,
            event_type=event_type,
            # 服务端 run_id 强制覆盖，payload 里的同名键不能伪造
            payload_json={**(payload or {}), "run_id": run_id},
            created_at=utc_now(),
        )
        self.db.add(event)
        await self.db.flush()
        await self.db.commit()
        await self.broker.publish(event)
        return event

    async def stream(
        self, run_id: str, user_id: str, last_event_id: int
    ) -> AsyncIterator[AgentEvent]:
        """按 ``last_event_id``（per-run sequence）从 DB 重放，再跟随 broker。

        归属校验失败抛 :class:`RunEventForbidden`（路由层映射 404）。
        循环中先查库、再等 broker；broker 超时回到查库兜底（数据库是事实来源）。
        终态事件（``run.completed/failed/cancelled``）送达后流结束。

        每个 DB 轮询前 ``commit()`` 释放当前事务快照：MySQL REPEATABLE-READ 下
        快照在事务首查后固定，若不释放，跨进程/跨 worker 已提交的事件永远不可见，
        跨 worker 的终态事件将无法结束流。stream 在归属校验后只读，commit 不改数据。
        """
        run = await self.db.scalar(select(AgentRun).where(AgentRun.id == run_id))
        if run is None:
            raise RunEventForbidden("run_not_found")
        if run.user_id != user_id:
            raise RunEventForbidden("run_not_owned")
        queue = await self.broker.subscribe(run_id)
        seen = last_event_id
        try:
            while True:
                # 释放快照后查询，看到其它进程已提交的事件（初始重放也走这里）
                await self.db.commit()
                for row in await self._list_after(run_id, user_id, seen):
                    if row.sequence > seen:
                        seen = row.sequence
                        yield row
                        if is_terminal_event(row.event_type):
                            return
                try:
                    row = await asyncio.wait_for(queue.get(), timeout=0.5)
                except TimeoutError:
                    # 同进程 append 会经 broker 唤醒；超时说明跨进程写入，
                    # 回到循环顶部的查库补齐。超时轮询本身充当心跳。
                    continue
                if row.user_id == user_id and row.sequence > seen:
                    seen = row.sequence
                    yield row
                    if is_terminal_event(row.event_type):
                        return
        finally:
            await self.broker.unsubscribe(run_id, queue)

    async def _list_after(
        self, run_id: str, user_id: str, seen: int
    ) -> list[AgentEvent]:
        return list(
            (
                await self.db.scalars(
                    select(AgentEvent)
                    .where(
                        AgentEvent.run_id == run_id,
                        AgentEvent.user_id == user_id,
                        AgentEvent.sequence > seen,
                    )
                    .order_by(AgentEvent.sequence)
                )
            ).all()
        )
