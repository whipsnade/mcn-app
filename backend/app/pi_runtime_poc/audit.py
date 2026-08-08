"""Pi POC 审计写入的单一锁序。

同一个 Run 的 RPC Step、DataTap ToolCall 与产品 Event 必须先锁 ``agent_runs``，再分配
子表序号；否则 ``max(sequence)`` 的唯一索引与 Event 写入的 Run 行锁会形成死锁环。
"""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.events import AgentEventStream, PiRpcMappedEvent, map_pi_rpc_event
from app.agent_runtime.evidence import EvidenceWriter
from app.agent_runtime.models import (
    AgentEvent,
    AgentRun,
    AgentRunAttempt,
    AgentStep,
    AgentToolCall,
    EvidenceItem,
)
from app.core.redaction import redact_for_log

_SERVICE_NAME = "pi_poc_datatap"
_STEP_TYPE = "tool_call"


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def logical_call_id(run_id: str, pi_call_id: str) -> str:
    return hashlib.sha256(f"{run_id}\x00{pi_call_id}".encode()).hexdigest()


def arguments_hash(arguments: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(arguments, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class PiRunAuditWriter:
    """同一 Pi Run 的 Step/ToolCall/Event 原子写入器。"""

    def __init__(self, *, db: AsyncSession, events: AgentEventStream) -> None:
        self._db = db
        self._events = events

    async def write_rpc_event(self, *, run_id: str, event: dict[str, Any]) -> None:
        run = await self._lock_run(run_id)
        attempt = await self._ensure_attempt(run.id)
        await self._add_step(
            run=run,
            attempt_id=attempt.id,
            step_type="pi_rpc_event",
            input_json={"event": redact_for_log(event)},
            status="completed",
        )
        mapped = map_pi_rpc_event(event)
        product_event = await self._append_mapped_event(run, mapped)
        await self._commit(product_event)

    async def write_thinking_chunk(self, *, run_id: str, text: str) -> None:
        if not text:
            return
        run = await self._lock_run(run_id)
        attempt = await self._ensure_attempt(run.id)
        await self._add_step(
            run=run,
            attempt_id=attempt.id,
            step_type="pi_rpc_thinking_chunk",
            input_json={},
            status="completed",
            thinking_text=text,
        )
        product_event = await self._events.append_locked(
            run,
            "thinking.delta",
            {"text": text, "collapsed": True},
        )
        await self._commit(product_event)

    async def write_extension_diagnostic(
        self, *, run_id: str, diagnostic: dict[str, str | None]
    ) -> None:
        """记录 Extension 生命周期的安全阶段，不写原始 Node/MCP 错误。"""
        run = await self._lock_run(run_id)
        attempt = await self._ensure_attempt(run.id)
        await self._add_step(
            run=run,
            attempt_id=attempt.id,
            step_type="pi_extension_diagnostic",
            input_json={"diagnostic": diagnostic},
            status="completed",
        )
        await self._commit(None)

    async def start_tool(
        self,
        *,
        run_id: str,
        pi_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        requested_tool_name: str | None = None,
        service_name: str | None = None,
    ) -> str:
        run = await self._lock_run(run_id)
        call_key = logical_call_id(run_id, pi_call_id)
        existing = await self._db.scalar(
            select(AgentToolCall).where(AgentToolCall.logical_call_id == call_key)
        )
        if existing is not None:
            await self._db.commit()
            return existing.id

        attempt = await self._ensure_attempt(run.id)
        step = await self._add_step(
            run=run,
            attempt_id=attempt.id,
            step_type=_STEP_TYPE,
            input_json={
                "internal_tool_name": tool_name,
                "requested_tool_name": requested_tool_name or tool_name,
                "service_name": service_name,
                "arguments": arguments,
            },
            status="running",
        )
        call = AgentToolCall(
            id=str(uuid4()),
            run_id=run.id,
            step_id=step.id,
            logical_call_id=call_key,
            service=_SERVICE_NAME,
            internal_tool_name=tool_name,
            arguments_json=arguments,
            arguments_hash=arguments_hash(arguments),
            status="running",
            points_reserved=0,
            points_settled=0,
        )
        self._db.add(call)
        await self._db.flush()
        product_event = await self._events.append_locked(
            run,
            "tool.started",
            {"tool_name": tool_name, "call_id": call.id},
        )
        await self._commit(product_event)
        return call.id

    async def settle_tool(self, *, run_id: str, call_id: str, raw_payload: Any) -> str | None:
        """原子结算一条 Pi DataTap Call。

        必须从 Run 行锁开始；不能先读/写 ToolCall 或 Evidence 再调用 EventStream，
        否则会和 RPC Step 写入形成 ``agent_steps -> agent_runs`` 的反向锁序。
        """
        run = await self._lock_run(run_id)
        call = await self._owned_call(run.id, call_id)
        if call.status == "settled":
            evidence = await self._db.scalar(
                select(EvidenceItem).where(EvidenceItem.tool_call_id == call.id)
            )
            await self._db.commit()
            return evidence.id if evidence is not None else None
        if call.status not in ("running", "planned"):
            raise ValueError("pi_tool_call_not_settleable")

        evidence = await EvidenceWriter(self._db).write(
            session_id=run.session_id,
            run_id=run.id,
            tool_call_id=call.id,
            source_type="datatap_mcp",
            source_name=call.internal_tool_name,
            scope_json=None,
            period_json=None,
            raw_payload=raw_payload,
            availability_status="available",
        )
        call.status = "settled"
        call.completed_at = _now()
        await self._db.flush()
        event = await self._events.append_locked(
            run,
            "tool.succeeded",
            {"tool_name": call.internal_tool_name, "call_id": call.id, "evidence_id": evidence.id},
        )
        await self._commit(event)
        return evidence.id

    async def fail_tool(
        self,
        *,
        run_id: str,
        call_id: str,
        status: str,
        safe_error_message: str | None,
    ) -> None:
        """原子标记 failed/unknown，沿用 Run-first 锁序。"""
        run = await self._lock_run(run_id)
        call = await self._owned_call(run.id, call_id)
        if call.status in ("settled", "failed", "unknown"):
            await self._db.commit()
            return
        call.status = status
        call.error_type = f"pi_poc_{status}"
        call.safe_error_message = safe_error_message
        call.completed_at = _now()
        await self._db.flush()
        event_type = "tool.failed" if status == "failed" else "tool.unknown"
        event = await self._events.append_locked(
            run,
            event_type,
            {"tool_name": call.internal_tool_name, "call_id": call.id},
        )
        await self._commit(event)

    async def _lock_run(self, run_id: str) -> AgentRun:
        run = await self._db.scalar(select(AgentRun).where(AgentRun.id == run_id).with_for_update())
        if run is None:
            raise LookupError("pi_run_not_found")
        return run

    async def _owned_call(self, run_id: str, call_id: str) -> AgentToolCall:
        call = await self._db.scalar(
            select(AgentToolCall)
            .where(AgentToolCall.id == call_id, AgentToolCall.run_id == run_id)
            .with_for_update()
        )
        if call is None:
            raise LookupError("pi_tool_call_not_found")
        return call

    async def _ensure_attempt(self, run_id: str) -> AgentRunAttempt:
        attempt = await self._db.scalar(
            select(AgentRunAttempt)
            .where(AgentRunAttempt.run_id == run_id)
            .order_by(AgentRunAttempt.attempt.desc())
            .limit(1)
            .with_for_update()
        )
        if attempt is not None:
            return attempt
        attempt = AgentRunAttempt(
            id=str(uuid4()),
            run_id=run_id,
            attempt=1,
            started_at=_now(),
            decision_count=0,
            outcome="running",
        )
        self._db.add(attempt)
        await self._db.flush()
        return attempt

    async def _add_step(
        self,
        *,
        run: AgentRun,
        attempt_id: str,
        step_type: str,
        input_json: dict[str, Any],
        status: str,
        thinking_text: str | None = None,
    ) -> AgentStep:
        sequence = await self._db.scalar(
            select(func.max(AgentStep.sequence))
            .where(AgentStep.run_id == run.id)
            .with_for_update()
        )
        step = AgentStep(
            id=str(uuid4()),
            run_id=run.id,
            attempt_id=attempt_id,
            sequence=(sequence or 0) + 1,
            step_type=step_type,
            input_json=input_json,
            status=status,
            thinking_text=thinking_text,
            visibility=run.visibility,
            created_at=_now(),
        )
        self._db.add(step)
        await self._db.flush()
        return step

    async def _append_mapped_event(
        self, run: AgentRun, mapped: PiRpcMappedEvent | None
    ) -> AgentEvent | None:
        if mapped is None:
            return None
        return await self._events.append_locked(run, mapped.event_type, mapped.payload)

    async def _commit(self, event: AgentEvent | None) -> None:
        if event is None:
            await self._db.commit()
            return
        await self._events.commit_and_publish(event)
