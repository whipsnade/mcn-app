"""Internal FastAPI routes used only by an authenticated Pi Gateway."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from app.agent_runtime.events import AgentEventStream
from app.agent_runtime.models import AgentEvent, AgentRunAttempt, AgentSession
from app.agent_runtime.state import InvalidRunTransition, RunStatus
from app.db.session import get_db
from app.core.config import get_settings

from .auth import PiGatewayAuthError, verify_signed_request
from .contracts import (
    PiGatewayClaimRequest,
    PiGatewayHeartbeatRequest,
    PiGatewayInternalToolRequest,
    PiGatewaySourceEvent,
    PiGatewayTerminalRequest,
)
from .internal_tools import ProductionInternalToolBridge
from .models import PiGatewayRequestNonce
from .service import PiGatewayClaimError, PiGatewayLeaseError, PiGatewayService


router = APIRouter()
def _auth_error() -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="pi_gateway_auth_failed")


async def _authenticate(request: Request, body: bytes, db: AsyncSession) -> str:
    settings = get_settings()
    secret = settings.pi_gateway_internal_secret.get_secret_value()
    if not secret or not settings.pi_gateway_allowed_ids:
        raise _auth_error()
    try:
        verified = verify_signed_request(
            request.headers,
            method=request.method,
            path=request.url.path,
            body=body,
            secret=secret,
            allowed_gateway_ids=set(settings.pi_gateway_allowed_ids),
            nonce_store=None,
        )
    except PiGatewayAuthError as exc:
        # Do not expose the distinction between unknown gateway, bad HMAC and replay.
        raise _auth_error() from exc
    now = datetime.now(UTC).replace(tzinfo=None)
    await db.execute(delete(PiGatewayRequestNonce).where(PiGatewayRequestNonce.expires_at <= now))
    db.add(
        PiGatewayRequestNonce(
            id=str(uuid4()),
            gateway_id=verified.gateway_id,
            nonce=verified.nonce,
            expires_at=now + timedelta(seconds=30),
            created_at=now,
        )
    )
    try:
        await db.flush()
        # Commit the authentication transaction before business work starts so
        # a failed claim/lease operation cannot roll back the replay barrier.
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise _auth_error() from exc
    return verified.gateway_id


def _service(db: AsyncSession, gateway_id: str) -> PiGatewayService:
    return PiGatewayService(
        db,
        gateway_id=gateway_id,
        lease_seconds=get_settings().pi_gateway_lease_seconds,
    )


@router.post("/claims")
async def claim(
    request: Request,
    payload: PiGatewayClaimRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Any:
    gateway_id = await _authenticate(request, await request.body(), db)
    try:
        result = await _service(db, gateway_id).claim_next(payload)
    except PiGatewayClaimError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.code) from exc
    if result is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return result.model_dump(mode="json")


async def _leased(
    request: Request,
    db: AsyncSession,
    run_id: str,
    attempt_id: str | None,
    lease: str | None,
) -> tuple[str, object]:
    if not lease:
        raise _auth_error()
    gateway_id = await _authenticate(request, await request.body(), db)
    if attempt_id is None:
        attempt_id = await db.scalar(
            select(AgentRunAttempt.id)
            .where(AgentRunAttempt.run_id == run_id, AgentRunAttempt.outcome == "running")
            .order_by(AgentRunAttempt.attempt.desc())
            .limit(1)
        )
    if not attempt_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="pi_gateway_run_not_found")
    try:
        run = await _service(db, gateway_id).leased_run(run_id, attempt_id, lease)
    except PiGatewayLeaseError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="pi_gateway_run_not_found") from exc
    return gateway_id, run


@router.post("/runs/{run_id}/heartbeat")
async def heartbeat(
    run_id: str,
    request: Request,
    payload: PiGatewayHeartbeatRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    x_pi_run_lease: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    _gateway_id, run = await _leased(request, db, run_id, payload.attempt_id, x_pi_run_lease)
    service = _service(db, _gateway_id)
    now = service.now_fn()
    run.heartbeat_at = now
    run.gateway_lease_expires_at = now + timedelta(seconds=service.lease_seconds)
    run.lease_expires_at = run.gateway_lease_expires_at
    await db.commit()
    return {"ok": True, "cancel_requested": bool(run.cancel_requested)}


@router.post("/runs/{run_id}/events")
async def source_event(
    run_id: str,
    request: Request,
    payload: PiGatewaySourceEvent,
    db: Annotated[AsyncSession, Depends(get_db)],
    x_pi_run_lease: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    _gateway_id, run = await _leased(request, db, run_id, payload.source_event_id.split(":", 1)[0], x_pi_run_lease)
    existing = await db.scalar(
        select(AgentEvent).where(
            AgentEvent.run_id == run.id,
            AgentEvent.source_event_id == payload.source_event_id,
        )
    )
    if existing is not None:
        await db.commit()
        return {"event_id": existing.id, "sequence": existing.sequence, "duplicate": True}
    stream = AgentEventStream(db, request.app.state.agent_event_broker)
    event = await stream.append_locked(
        run, payload.event_type, {**payload.payload, "source_event_id": payload.source_event_id}
    )
    event.source_event_id = payload.source_event_id
    await stream.commit_and_publish(event)
    return {"event_id": event.id, "sequence": event.sequence, "duplicate": False}


@router.post("/runs/{run_id}/internal-tools")
async def internal_tool(
    run_id: str,
    request: Request,
    payload: PiGatewayInternalToolRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    x_pi_run_lease: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    gateway_id, run = await _leased(request, db, run_id, None, x_pi_run_lease)
    bridge = ProductionInternalToolBridge(db=db, worker_id=gateway_id)
    result = await bridge.execute(
        tool_name=payload.tool_name,
        arguments=payload.args,
        user_id=run.user_id,
        session_id=run.session_id,
        run_id=run.id,
        profile_name=run.profile_name,
    )
    await db.commit()
    return result.model_dump(mode="json")


@router.post("/runs/{run_id}/terminal")
async def terminal(
    run_id: str,
    request: Request,
    payload: PiGatewayTerminalRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    x_pi_run_lease: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    _gateway_id, run = await _leased(request, db, run_id, payload.attempt_id, x_pi_run_lease)
    outcome = RunStatus(payload.outcome)
    stream = AgentEventStream(db, request.app.state.agent_event_broker)

    async def cleanup_before_commit(locked_run) -> None:
        attempt = await db.scalar(
            select(AgentRunAttempt)
            .where(AgentRunAttempt.id == payload.attempt_id, AgentRunAttempt.run_id == locked_run.id)
            .with_for_update()
        )
        if attempt is not None and attempt.outcome == "running":
            attempt.outcome = "completed" if outcome == RunStatus.COMPLETED_WITH_WARNINGS else outcome.value
            attempt.ended_at = _service(db, _gateway_id).now_fn()
        session = await db.scalar(
            select(AgentSession).where(AgentSession.id == locked_run.session_id).with_for_update()
        )
        if session is not None and session.active_run_id == locked_run.id:
            session.active_run_id = None
        locked_run.gateway_lease_hash = None
        locked_run.gateway_lease_expires_at = None
        locked_run.gateway_id = None
        locked_run.lease_owner = None
        locked_run.lease_expires_at = None

    try:
        event = await stream.settle_terminal(
            run.id,
            run.user_id,
            outcome,
            payload.payload,
            worker_id=_gateway_id,
            before_commit=cleanup_before_commit,
        )
    except InvalidRunTransition as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="pi_gateway_terminal_rejected") from exc
    return {"event_id": event.id if event else None, "status": outcome.value}
