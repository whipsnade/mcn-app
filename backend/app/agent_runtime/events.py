"""统一 Agent Run 的持久事件流（spec §15.3）。

事件持久化到 ``agent_events``（``run_id`` + per-run ``sequence`` 唯一），
通过进程内 ``AgentEventBroker`` 唤醒订阅方；数据库是唯一事实来源，
broker 只承担"有新事件"的唤醒信号。

事件 payload 约定（spec §15.3）：
- 所有事件 payload 必须带 ``run_id``（服务端强制写入，客户端不可伪造）；
- Artifact 事件带 ``artifact_id/module/parent_artifact_id/status``；
- Review 事件带 ``review_batch_id/artifact_id/draft_revision_id``。
``append`` 会自动把 ``run_id`` 合入 payload，调用方无需重复携带。

终态收口（H1/§5.8）：``settle_terminal`` 是唯一事务边界——Run 状态迁移
与终态事件在同一 ``SELECT ... FOR UPDATE`` 加锁事务内提交，恰好一个
``run.completed|failed|cancelled``；``append_terminal_once`` 共享同一
持锁复核，只做事件补发（不迁移状态）。
"""

import asyncio
import logging
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.models import AgentEvent, AgentRun
from app.agent_runtime.repository import AgentRunRepository
from app.agent_runtime.state import (
    TERMINAL_RUN_STATUSES,
    InvalidRunTransition,
    RunStatus,
)
from app.core.redaction import redact_for_log


logger = logging.getLogger(__name__)


class AgentEventType(StrEnum):
    RUN_STARTED = "run.started"
    RUN_PAUSED = "run.paused"
    RUN_RESUMED = "run.resumed"
    RUN_COMPLETED = "run.completed"
    RUN_COMPLETED_WITH_WARNINGS = "run.completed_with_warnings"
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
    MESSAGE_DELTA = "message.delta"
    MESSAGE_COMPLETED = "message.completed"


# 终态事件：送达后流必须结束，前端据此判定 Run 已出结果。
TERMINAL_EVENT_TYPES = frozenset(
    {
        AgentEventType.RUN_COMPLETED,
        AgentEventType.RUN_COMPLETED_WITH_WARNINGS,
        AgentEventType.RUN_FAILED,
        AgentEventType.RUN_CANCELLED,
    }
)


def is_terminal_event(event_type: str) -> bool:
    return event_type in TERMINAL_EVENT_TYPES


@dataclass(frozen=True)
class PiRpcMappedEvent:
    """Pi 原始 RPC 事件的最小产品投影；不携带参数、原始结果或凭证。"""

    event_type: AgentEventType
    payload: dict[str, Any]


def map_pi_rpc_event(event: dict[str, Any]) -> PiRpcMappedEvent | None:
    """把已验证的 Pi RPC 事件映射为稳定产品事件。

    未识别事件仅由 Runner 写入 Step 审计，绝不作为前端产品事件；工具参数和原始结果
    同样只留在 Pi/DataTap 的受控审计链路，不进入事件 payload。
    """
    event_type = event.get("type")
    if event_type == "agent_start":
        return PiRpcMappedEvent(AgentEventType.THINKING_STARTED, {"collapsed": True})
    if event_type == "agent_end" and event.get("willRetry") is False:
        return PiRpcMappedEvent(AgentEventType.THINKING_COMPLETED, {"collapsed": True})
    if event_type == "error":
        # Pi 的原始错误全文仅保留在 Step 审计；SSE 只暴露稳定分类，避免供应商
        # 诊断、Authorization 或其他敏感片段进入产品事件。
        return PiRpcMappedEvent(
            AgentEventType.THINKING_FAILED,
            {"code": "pi_rpc_error", "collapsed": True},
        )
    if event_type == "message_update":
        update = event.get("assistantMessageEvent")
        if not isinstance(update, dict):
            return None
        delta_type = update.get("type")
        delta = update.get("delta")
        if not isinstance(delta, str) or not delta:
            return None
        if delta_type == "thinking_delta":
            return PiRpcMappedEvent(
                AgentEventType.THINKING_DELTA,
                {"text": redact_for_log(delta), "collapsed": True},
            )
        if delta_type == "text_delta":
            return PiRpcMappedEvent(
                AgentEventType.MESSAGE_DELTA,
                {"text": redact_for_log(delta)},
            )
        return None
    if event_type in ("tool_execution_start", "tool_execution_end"):
        call_id = event.get("toolCallId")
        tool_name = event.get("toolName")
        if not isinstance(call_id, str) or not isinstance(tool_name, str):
            return None
        if event_type == "tool_execution_start":
            product_type = AgentEventType.TOOL_STARTED
        elif event.get("isError") is True:
            product_type = AgentEventType.TOOL_FAILED
        else:
            product_type = AgentEventType.TOOL_SUCCEEDED
        return PiRpcMappedEvent(
            product_type,
            {"call_id": call_id, "tool_name": redact_for_log(tool_name)},
        )
    return None


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
        event = await self.append_locked(run, event_type, payload)
        await self.commit_and_publish(event)
        return event

    async def append_locked(
        self, run: AgentRun, event_type: str, payload: dict[str, Any] | None
    ) -> AgentEvent:
        """在调用方已锁定 Run 行的事务中写入 Event，但不提交或广播。"""
        max_sequence = await self.db.scalar(
            select(func.max(AgentEvent.sequence))
            .where(AgentEvent.run_id == run.id)
            .with_for_update()
        )
        event = AgentEvent(
            id=str(uuid4()),
            run_id=run.id,
            user_id=run.user_id,
            sequence=(max_sequence or 0) + 1,
            event_type=event_type,
            # 服务端 run_id 强制覆盖，payload 里的同名键不能伪造
            payload_json={**(payload or {}), "run_id": run.id},
            created_at=utc_now(),
        )
        self.db.add(event)
        await self.db.flush()
        return event

    async def commit_and_publish(self, event: AgentEvent) -> None:
        """提交由 :meth:`append_locked` 写入的事件，并在提交后唤醒订阅者。"""
        await self.db.commit()
        await self.broker.publish(event)

    async def append_terminal_once(
        self,
        run_id: str,
        user_id: str,
        event_type: str,
        payload: dict[str, Any] | None,
        *,
        completion_validator: Callable[[AgentRun], Awaitable[object]] | None = None,
    ) -> AgentEvent | None:
        """终态事件补发出口（G1/§5.8）：同一 Run 全局恰好一个终态事件。

        复核与插入在 Run 行锁内完成（与 :meth:`settle_terminal` 共享同一持锁
        边界）：并发写者被行锁串行化，后到者看到已有终态事件幂等返回
        ``None``，绝不重复发送、不抛唯一键异常；非终态类型抛 ``ValueError``
        （调用方编排错误，不应静默）。本方法只补发事件、不迁移 Run 状态——
        Run 状态迁移由 :meth:`settle_terminal` 负责。
        """
        if not is_terminal_event(event_type):
            raise ValueError(f"not a terminal event: {event_type!r}")
        run = await self._lock_run_for_update(run_id)
        if run.user_id != user_id:
            raise RunEventForbidden("run_not_owned")
        if await self._terminal_event_locked(run_id) is not None:
            await self.db.commit()
            return None
        if run.status in (
            RunStatus.COMPLETED.value,
            RunStatus.COMPLETED_WITH_WARNINGS.value,
        ):
            if completion_validator is None:
                from app.pi_gateway.completion import CompletionValidator

                completion_validator = CompletionValidator(self.db).validate
            validation = await completion_validator(run)
            if not bool(validation):
                code = getattr(validation, "code", None)
                raise InvalidRunTransition(
                    code if isinstance(code, str) and code else "completion_validation_failed"
                )
        event = await self._insert_terminal_locked(run, event_type, payload)
        await self.db.commit()
        await self.broker.publish(event)
        return event

    async def settle_terminal(
        self,
        run_id: str,
        user_id: str,
        outcome: RunStatus,
        payload: dict[str, Any] | None,
        *,
        worker_id: str | None = None,
        before_commit: Callable[[AgentRun], Awaitable[None]] | None = None,
        allow_system_completion: Callable[[AgentRun], Awaitable[bool]] | None = None,
        completion_validator: Callable[[AgentRun], Awaitable[object]] | None = None,
    ) -> AgentEvent | None:
        """Run 终态收口唯一事务边界（H1/§5.8）：状态迁移与终态事件同一加锁事务。

        事务内：``SELECT ... FOR UPDATE`` 锁 Run 行（串行化所有并发写者）→
        当前读复核终态事件（已存在则幂等返回 ``None``）→ Run 未终态则按
        ``outcome`` 迁移 → 插入终态事件（sequence=max+1）→ 提交 → broker 广播。
        任何中途异常整体回滚——不存在"Run 已终态但无终态事件"的提交窗口；
        并发后到者被行锁串行化后看到已有终态，幂等返回而非唯一键异常。

        ``worker_id`` 语义：COMPLETED/COMPLETED_WITH_WARNINGS 必须由持活跃租约的
        worker 迁移（否则抛 ``run_lease_not_held``）；FAILED 持租约走状态机、无租约
        走系统级 force_fail（他人活跃持有时返回 ``None``，终态交接管方，A4 闸门）；
        CANCELLED 是用户取消跨切面语义（任意非终态 → cancelled，不看租约）。
        Run 已是终态但缺终态事件（旧窗口残留）时按实际终态补发事件，不再迁移。
        幂等与闸门路径同样提交：调用方在本事务内可能已 flush 需要落库的写入
        （如 _fail_run 的 transition / Draft 释放），本方法是统一提交点。
        """
        if outcome not in TERMINAL_RUN_STATUSES:
            raise ValueError(f"not a terminal outcome: {outcome!r}")
        run = await self._lock_run_for_update(run_id)
        if run.user_id != user_id:
            raise RunEventForbidden("run_not_owned")
        if await self._terminal_event_locked(run_id) is not None:
            if before_commit is not None:
                await before_commit(run)
            await self.db.commit()
            await self._reconcile_terminal(run.id)
            return None
        current = RunStatus(run.status)
        if current in TERMINAL_RUN_STATUSES:
            # 旧窗口残留（Run 已终态、无终态事件）：按实际终态补发，不再迁移。
            if current in (
                RunStatus.COMPLETED,
                RunStatus.COMPLETED_WITH_WARNINGS,
            ):
                if completion_validator is None:
                    from app.pi_gateway.completion import CompletionValidator

                    completion_validator = CompletionValidator(self.db).validate
                validation = await completion_validator(run)
                if not bool(validation):
                    code = getattr(validation, "code", None)
                    raise InvalidRunTransition(
                        code if isinstance(code, str) and code else "completion_validation_failed"
                    )
            event_type = f"run.{current.value}"
        else:
            migrated = await self._migrate_terminal_locked(
                run,
                outcome,
                worker_id,
                payload,
                allow_system_completion,
                completion_validator,
            )
            if not migrated:
                await self.db.commit()
                return None
            event_type = f"run.{outcome.value}"
        event = await self._insert_terminal_locked(run, event_type, payload)
        if before_commit is not None:
            await before_commit(run)
        await self.db.commit()
        await self.broker.publish(event)
        await self._reconcile_terminal(run.id)
        return event

    async def _reconcile_terminal(self, run_id: str) -> None:
        """Run the read-only usage/ledger audit for every runtime backend.

        The event stream is the shared terminal boundary for current, Pi and
        historical POC runs.  Reconciliation is deliberately best-effort and
        never reopens or mutates a terminal Run when a legacy fixture has no
        tenant ledger yet.
        """

        try:
            from app.pi_gateway.accounting import RuntimeUsageService

            result = await RuntimeUsageService(self.db).reconcile_run(run_id)
            if result.reconciliation_status == "mismatch":
                logger.warning(
                    "runtime usage reconciliation mismatch run=%s codes=%s",
                    run_id,
                    ",".join(result.mismatch_codes) or "unknown",
                )
        except Exception as exc:
            code = getattr(exc, "code", None)
            logger.warning(
                "runtime usage reconciliation unavailable run=%s code=%s",
                run_id,
                code if isinstance(code, str) else "runtime_usage_reconciliation_failed",
            )
            return

    async def _lock_run_for_update(self, run_id: str) -> AgentRun:
        """持锁读 Run 行并刷新 identity map（populate_existing）。

        行锁是终态收口的串行化点：所有终态写者先过此锁；locking read 不受
        REPEATABLE-READ 快照限制，读到的是最新已提交状态。
        """
        run = await self.db.scalar(
            select(AgentRun)
            .where(AgentRun.id == run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if run is None:
            raise RunEventForbidden("run_not_found")
        return run

    async def _terminal_event_locked(self, run_id: str) -> AgentEvent | None:
        """当前读复核已有终态事件（调用方必须已持 Run 行锁）。

        ``FOR UPDATE`` 当前读绕开事务快照：并发写者的终态事件（其提交必然
        先于本事务获锁）不会被旧快照遮蔽——这是"先查后写"竞态的根治。
        """
        return await self.db.scalar(
            select(AgentEvent)
            .where(
                AgentEvent.run_id == run_id,
                AgentEvent.event_type.in_(tuple(sorted(TERMINAL_EVENT_TYPES))),
            )
            .order_by(AgentEvent.sequence)
            .limit(1)
            .with_for_update()
        )

    async def _insert_terminal_locked(
        self, run: AgentRun, event_type: str, payload: dict[str, Any] | None
    ) -> AgentEvent:
        """在持锁事务内插入终态事件（sequence=max+1）并 flush（不 commit）。

        max 查询用当前读：调用方会话若在持锁前已有一致性读快照（如路由层
        归属查询），旧快照可能漏掉持锁前并发提交的非终态事件，导致 sequence
        撞唯一键；当前读保证看到的是获锁后的最新数据。
        """
        max_sequence = await self.db.scalar(
            select(func.max(AgentEvent.sequence))
            .where(AgentEvent.run_id == run.id)
            .with_for_update()
        )
        event = AgentEvent(
            id=str(uuid4()),
            run_id=run.id,
            user_id=run.user_id,
            sequence=(max_sequence or 0) + 1,
            event_type=event_type,
            # 服务端 run_id 强制覆盖，payload 里的同名键不能伪造
            payload_json={**(payload or {}), "run_id": run.id},
            created_at=utc_now(),
        )
        self.db.add(event)
        await self.db.flush()
        return event

    async def _migrate_terminal_locked(
        self,
        run: AgentRun,
        outcome: RunStatus,
        worker_id: str | None,
        payload: dict[str, Any] | None,
        allow_system_completion: Callable[[AgentRun], Awaitable[bool]] | None = None,
        completion_validator: Callable[[AgentRun], Awaitable[object]] | None = None,
    ) -> bool:
        """持锁状态下按 outcome 迁移 Run 终态（不 commit）。

        返回 ``False`` 表示租约被其他 worker 活跃持有（A4 闸门）：本 worker
        不迁移、不发事件，终态交接管方。repo 方法内的 lock_run 是同事务
        重入行锁（立即成功），且 identity map 对象已被持锁读刷新。
        """
        repo = AgentRunRepository(self.db)
        if outcome == RunStatus.CANCELLED:
            # 用户取消跨切面：任意非终态 → cancelled，不看租约。
            from app.pi_gateway.completion import close_open_runtime_rows

            await close_open_runtime_rows(
                self.db,
                run.id,
                utc_now(),
                error_code="cancel_requested",
            )
            return await repo.cancel(run.id, run.user_id)
        if outcome in (RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_WARNINGS):
            if completion_validator is None:
                # 所有成功终态都必须经过统一业务完成契约；保留动态导入避免
                # agent_runtime 与 pi_gateway 形成模块级循环依赖。没有 required
                # artifact 的旧 profile 仍会由 validator 校验 message/Step/MCP
                # 生命周期，而不会回退为 assistant-only completion。
                from app.pi_gateway.completion import CompletionValidator

                completion_validator = CompletionValidator(self.db).validate

            async def validate_completion() -> None:
                if completion_validator is None:
                    raise InvalidRunTransition("completion_validation_required")
                result = await completion_validator(run)
                if not bool(result):
                    code = getattr(result, "code", None)
                    raise InvalidRunTransition(
                        code if isinstance(code, str) and code else "completion_validation_failed"
                    )

            if worker_id is not None and AgentRunRepository.owns_active_lease(run, worker_id):
                await validate_completion()
                await repo.transition(
                    run.id,
                    outcome,
                    worker_id=worker_id,
                    completion_validator=completion_validator,
                )
                return True
            if (
                worker_id is not None
                and run.lease_owner is not None
                and run.lease_expires_at is not None
                and run.lease_expires_at > utc_now()
            ):
                # 另一个仍存活的 worker 持有租约时，普通 terminal 请求不能
                # 退化成 system force-complete；只有租约失效后的 Recovery 才能
                # 进入无 worker 的 ACK-lost 收口路径。
                raise InvalidRunTransition("run_lease_not_held")
            # 系统级完成收口同样必须过完整业务契约。旧的
            # allow_system_completion 只允许作为兼容调用信号，不能绕过校验。
            if allow_system_completion is not None and await allow_system_completion(run):
                await validate_completion()
                return await repo.force_complete(
                    run.id, outcome=outcome, completion_validator=completion_validator
                )
            if completion_validator is not None:
                await validate_completion()
                return await repo.force_complete(
                    run.id, outcome=outcome, completion_validator=completion_validator
                )
            raise InvalidRunTransition("run_lease_not_held")
        # FAILED：持租约走状态机；无租约/过期走系统级 force_fail（error_code 落 Run 行）。
        if worker_id is not None and AgentRunRepository.owns_active_lease(run, worker_id):
            from app.pi_gateway.completion import close_open_runtime_rows

            await close_open_runtime_rows(
                self.db,
                run.id,
                utc_now(),
                error_code=(payload or {}).get("error_code", "run_failed"),
            )
            await repo.transition(run.id, RunStatus.FAILED, worker_id=worker_id)
            return True
        return await repo.force_fail(run.id, error_code=(payload or {}).get("error_code"))

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
