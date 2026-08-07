"""Pi POC Evidence 旁路服务：start/settle/fail 状态机，零积分。

只处理 Pi 工具调用的可审计旁路：start 落 AgentStep + AgentToolCall（零积分、
logical_call_id = run_id + Pi call id，不依赖参数 hash 熔断），settle 写不可变
EvidenceItem，fail/unknown 不产生 available Evidence。不接入 DataTap、Producer、
正式 Runtime 或积分。
"""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.events import AgentEventStream
from app.agent_runtime.evidence import EvidenceWriter
from app.agent_runtime.models import (
    AgentRun,
    AgentRunAttempt,
    AgentStep,
    AgentToolCall,
    EvidenceItem,
)
from app.agent_runtime.repository import AgentRunRepository
from app.agent_runtime.tools.registry import UnknownToolError
from app.core.config import Settings
from app.pi_runtime_poc.auth import verify_run_token
from app.pi_runtime_poc.internal_tools import (
    PIPOC_PROFILE,
    build_pi_internal_registry,
)
from app.pi_runtime_poc.schemas import (
    PiToolFailed,
    PiToolSettled,
    PiToolSettledResponse,
    PiToolStarted,
    PiToolStartedResponse,
)

_SERVICE_NAME = "pi_poc_datatap"
_STEP_TYPE = "tool_call"


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _logical_call_id(run_id: str, pi_call_id: str) -> str:
    """logical_call_id = run + Pi call id；不把参数 hash 当业务熔断键。"""
    return hashlib.sha256(f"{run_id}\x00{pi_call_id}".encode()).hexdigest()


def _arguments_hash(arguments: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(arguments, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class PiEvidenceIngestService:
    """零积分 Evidence 旁路状态机 + 受控内部工具桥（仅 POC 内部使用）。"""

    def __init__(
        self,
        *,
        db: AsyncSession,
        events: AgentEventStream,
        settings: Settings,
        worker_id: str = "pi-poc",
    ) -> None:
        self._db = db
        self._events = events
        self._settings = settings
        self._worker_id = worker_id

    async def execute_internal_tool(
        self, *, token: str, run_id: str, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """执行白名单内部工具；身份一律来自 token 对应 Run，伪造字段被 Registry 剥离。

        publish_artifacts 前确保当前 worker 持有 Run 活跃租约（发布前置条件）。
        """
        verify_run_token(token, run_id, settings=self._settings)
        run = await self._db.get(AgentRun, run_id)
        if run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "pi_run_not_found")
        registry = build_pi_internal_registry(db=self._db, worker_id=self._worker_id)
        if tool_name == "publish_artifacts":
            repo = AgentRunRepository(self._db)
            if not await repo.holds_lease(run_id, self._worker_id):
                await repo.claim_lease(
                    run_id, self._worker_id, self._settings.pi_runtime_poc_run_timeout_seconds
                )
        try:
            result = await registry.execute(
                internal_name=tool_name,
                arguments=arguments,
                user_id=run.user_id,
                session_id=run.session_id,
                run_id=run.id,
                profile=PIPOC_PROFILE,
                channel_permissions=(),
            )
        except UnknownToolError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "pi_internal_tool_not_found")
        return result.model_dump(mode="json")

    async def start_tool(
        self, *, token: str, run_id: str, request: PiToolStarted
    ) -> PiToolStartedResponse:
        verify_run_token(token, run_id, settings=self._settings)
        run = await self._db.get(AgentRun, run_id)
        if run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "pi_run_not_found")

        logical_call_id = _logical_call_id(run_id, request.call_id)
        existing = await self._db.scalar(
            select(AgentToolCall).where(AgentToolCall.logical_call_id == logical_call_id)
        )
        if existing is not None:
            return PiToolStartedResponse(call_id=existing.id)

        attempt = await self._ensure_attempt(run.id)
        step_sequence = await self._next_step_sequence(run.id)
        step = AgentStep(
            id=str(uuid4()),
            run_id=run.id,
            attempt_id=attempt.id,
            sequence=step_sequence,
            step_type=_STEP_TYPE,
            input_json={"internal_tool_name": request.tool_name, "arguments": request.arguments},
            status="running",
            visibility=run.visibility,
            created_at=_now(),
        )
        self._db.add(step)
        await self._db.flush()

        call = AgentToolCall(
            id=str(uuid4()),
            run_id=run.id,
            step_id=step.id,
            logical_call_id=logical_call_id,
            service=_SERVICE_NAME,
            internal_tool_name=request.tool_name,
            arguments_json=request.arguments,
            arguments_hash=_arguments_hash(request.arguments),
            status="running",
            points_reserved=0,
            points_settled=0,
        )
        self._db.add(call)
        await self._db.flush()

        await self._events.append(
            run.id,
            run.user_id,
            "tool.started",
            {"tool_name": request.tool_name, "call_id": call.id},
        )
        return PiToolStartedResponse(call_id=call.id)

    async def settle_tool(
        self, *, token: str, run_id: str, call_id: str, request: PiToolSettled
    ) -> PiToolSettledResponse:
        verify_run_token(token, run_id, settings=self._settings)
        call = await self._require_owned_call(run_id, call_id)

        if call.status == "settled":
            evidence = await self._db.scalar(
                select(EvidenceItem).where(EvidenceItem.tool_call_id == call.id)
            )
            return PiToolSettledResponse(evidence_id=evidence.id if evidence else None)
        if call.status not in ("running", "planned"):
            raise HTTPException(status.HTTP_409_CONFLICT, "pi_tool_call_not_settleable")

        run = await self._db.get(AgentRun, run_id)
        assert run is not None
        writer = EvidenceWriter(self._db)
        evidence = await writer.write(
            session_id=run.session_id,
            run_id=run.id,
            tool_call_id=call.id,
            source_type="datatap_mcp",
            source_name=call.internal_tool_name,
            scope_json=None,
            period_json=None,
            raw_payload=request.raw_payload,
            availability_status="available",
        )
        call.status = "settled"
        call.completed_at = _now()
        await self._db.flush()
        await self._events.append(
            run.id,
            run.user_id,
            "tool.succeeded",
            {"tool_name": call.internal_tool_name, "call_id": call.id, "evidence_id": evidence.id},
        )
        return PiToolSettledResponse(evidence_id=evidence.id)

    async def fail_tool(
        self, *, token: str, run_id: str, call_id: str, request: PiToolFailed
    ) -> None:
        verify_run_token(token, run_id, settings=self._settings)
        call = await self._require_owned_call(run_id, call_id)
        if call.status in ("settled", "failed", "unknown"):
            return
        run = await self._db.get(AgentRun, run_id)
        assert run is not None
        call.status = request.status
        call.error_type = f"pi_poc_{request.status}"
        call.safe_error_message = (
            json.dumps(request.error, ensure_ascii=False, default=str)
            if request.error is not None
            else None
        )
        call.completed_at = _now()
        await self._db.flush()
        event_type = "tool.failed" if request.status == "failed" else "tool.unknown"
        await self._events.append(
            run.id,
            run.user_id,
            event_type,
            {"tool_name": call.internal_tool_name, "call_id": call.id},
        )

    async def _require_owned_call(self, run_id: str, call_id: str) -> AgentToolCall:
        call = await self._db.get(AgentToolCall, call_id)
        if call is None or call.run_id != run_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "pi_tool_call_not_found")
        return call

    async def _ensure_attempt(self, run_id: str) -> AgentRunAttempt:
        attempt = await self._db.scalar(
            select(AgentRunAttempt)
            .where(AgentRunAttempt.run_id == run_id)
            .order_by(AgentRunAttempt.attempt.desc())
            .limit(1)
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

    async def _next_step_sequence(self, run_id: str) -> int:
        max_sequence = await self._db.scalar(
            select(func.max(AgentStep.sequence)).where(AgentStep.run_id == run_id)
        )
        return (max_sequence or 0) + 1
