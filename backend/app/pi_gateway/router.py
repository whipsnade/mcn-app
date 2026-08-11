"""Internal FastAPI routes used only by an authenticated Pi Gateway."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError, OperationalError

from app.agent_runtime.events import AgentEventStream
from app.agent_runtime.models import AgentEvent, AgentRun, AgentRunAttempt, AgentSession
from app.agent_runtime.state import InvalidRunTransition, RunStatus
from app.db.session import get_db
from app.core.config import get_settings

from .auth import PiGatewayAuthError, verify_signed_request
from .contracts import (
    PiGatewayClaimRequest,
    PiGatewayHeartbeatRequest,
    PiGatewayInternalToolRequest,
    PiGatewayMcpFailRequest,
    PiGatewayMcpFinalizeRequest,
    PiGatewayMcpPreflightRequest,
    PiGatewaySourceEvent,
    PiGatewayTerminalRequest,
)
from .accounting import RuntimeUsageError, RuntimeUsageService, TenantAccountingError
from .events import PiGatewayEventError, parse_source_event_id
from .internal_tools import ProductionInternalToolBridge
from .models import PiGatewayRequestNonce
from .service import PiGatewayClaimError, PiGatewayLeaseError, PiGatewayService, lease_deadline_epoch


router = APIRouter()

# InnoDB 死锁（1213）与锁等待超时（1205）：REPEATABLE READ 下并发写路径
# （heartbeat/preflight/finalize/events 共用 Run/Attempt/Wallet 行与间隙锁）
# 可能瞬时互锁；回滚后按有界退避重试是标准处置，语义不变（全部写路径幂等）。
_LOCK_RETRYABLE_ERRNOS = {1205, 1213}
_LOCK_RETRY_ATTEMPTS = 3


def _is_retryable_lock_error(exc: BaseException) -> bool:
    origin = getattr(exc, "orig", exc)
    args = getattr(origin, "args", ())
    return bool(args) and args[0] in _LOCK_RETRYABLE_ERRNOS


async def _with_lock_retry(db: AsyncSession, operation: Any) -> Any:
    """在幂等写操作上重试 InnoDB 死锁/锁等待；每次重试都是全新事务。"""
    for attempt in range(_LOCK_RETRY_ATTEMPTS):
        try:
            return await operation()
        except OperationalError as exc:
            if attempt + 1 >= _LOCK_RETRY_ATTEMPTS or not _is_retryable_lock_error(exc):
                raise
            await db.rollback()
            await asyncio.sleep(0.05 * (attempt + 1))
    return None

# HMAC 时间偏差上限（与 auth.verify_signed_request 默认值一致）。
_SIGNATURE_MAX_SKEW_SECONDS = 30


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _auth_error() -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="pi_gateway_auth_failed")


async def _authenticate(request: Request, body: bytes, db: AsyncSession) -> str:
    settings = get_settings()
    secret = settings.pi_gateway_internal_secret.get_secret_value()
    if not secret or not settings.pi_gateway_allowed_ids:
        raise _auth_error()
    now = _utc_now()
    try:
        verified = verify_signed_request(
            request.headers,
            method=request.method,
            path=request.url.path,
            body=body,
            secret=secret,
            allowed_gateway_ids=set(settings.pi_gateway_allowed_ids),
            nonce_store=None,
            # naive UTC -> epoch：先补回 tzinfo 再取 timestamp，避免按本地时区解释。
            now=int(now.replace(tzinfo=UTC).timestamp()),
        )
    except PiGatewayAuthError as exc:
        # Do not expose the distinction between unknown gateway, bad HMAC and replay.
        raise _auth_error() from exc
    await db.execute(delete(PiGatewayRequestNonce).where(PiGatewayRequestNonce.expires_at <= now))
    # The barrier row must outlive the *entire* signature acceptance window,
    # which is derived from the signed timestamp (client clock may be fast),
    # not from the server receive time.  +1s covers the inclusive skew edge.
    signed_at = datetime.fromtimestamp(verified.timestamp, UTC).replace(tzinfo=None)
    db.add(
        PiGatewayRequestNonce(
            id=str(uuid4()),
            gateway_id=verified.gateway_id,
            nonce=verified.nonce,
            expires_at=signed_at + timedelta(seconds=_SIGNATURE_MAX_SKEW_SECONDS + 1),
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

    async def _do() -> Any:
        try:
            return await _service(db, gateway_id).claim_next(payload)
        except PiGatewayClaimError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.code) from exc

    result = await _with_lock_retry(db, _do)
    if result is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return result.model_dump(mode="json")


async def _authenticate_run_access(
    request: Request,
    db: AsyncSession,
    lease: str | None,
) -> str:
    """只做 HMAC/nonce 认证；nonce 屏障单次有效，绝不在锁重试内重复调用。"""
    if not lease:
        raise _auth_error()
    return await _authenticate(request, await request.body(), db)


async def _leased_run(
    db: AsyncSession,
    gateway_id: str,
    run_id: str,
    attempt_id: str | None,
    lease: str,
) -> object:
    """租约校验（可随锁重试在新事务内重放）。"""
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
        return await _service(db, gateway_id).leased_run(run_id, attempt_id, lease)
    except PiGatewayLeaseError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="pi_gateway_run_not_found") from exc


async def _leased(
    request: Request,
    db: AsyncSession,
    run_id: str,
    attempt_id: str | None,
    lease: str | None,
) -> tuple[str, object]:
    gateway_id = await _authenticate_run_access(request, db, lease)
    assert lease is not None
    return gateway_id, await _leased_run(db, gateway_id, run_id, attempt_id, lease)


@router.post("/runs/{run_id}/heartbeat")
async def heartbeat(
    run_id: str,
    request: Request,
    payload: PiGatewayHeartbeatRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    x_pi_run_lease: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    if not x_pi_run_lease:
        raise _auth_error()
    gateway_id = await _authenticate(request, await request.body(), db)

    async def _do() -> dict[str, object]:
        try:
            decision = await _service(db, gateway_id).scheduler.heartbeat(
                run_id,
                payload.attempt_id,
                x_pi_run_lease,
                gateway_id=gateway_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="pi_gateway_run_not_found") from exc
        return {
            "ok": True,
            "cancel_requested": decision.cancel_requested,
            "lease_expires_at": lease_deadline_epoch(decision.lease_expires_at),
        }

    return await _with_lock_retry(db, _do)


@router.post("/runs/{run_id}/events")
async def source_event(
    run_id: str,
    request: Request,
    payload: PiGatewaySourceEvent,
    db: Annotated[AsyncSession, Depends(get_db)],
    x_pi_run_lease: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    try:
        attempt_id, _sequence = parse_source_event_id(payload.source_event_id)
    except PiGatewayEventError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.code) from exc
    gateway_id = await _authenticate_run_access(request, db, x_pi_run_lease)

    async def _do() -> dict[str, object]:
        run = await _leased_run(db, gateway_id, run_id, attempt_id, x_pi_run_lease or "")
        try:
            receipt = await _service(db, gateway_id).ingest_source_event(
                run,
                attempt_id=attempt_id,
                source_event_id=payload.source_event_id,
                sequence=payload.sequence,
                event_type=payload.event_type,
                payload=payload.payload,
                broker=request.app.state.agent_event_broker,
            )
            await db.commit()
            if not receipt.get("duplicate") and receipt.get("event_id"):
                event = await db.get(AgentEvent, receipt["event_id"])
                if event is not None:
                    await request.app.state.agent_event_broker.publish(event)
            return receipt
        except (PiGatewayEventError, RuntimeUsageError) as exc:
            await db.rollback()
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    return await _with_lock_retry(db, _do)


@router.post("/runs/{run_id}/internal-tools")
async def internal_tool(
    run_id: str,
    request: Request,
    payload: PiGatewayInternalToolRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    x_pi_run_lease: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    gateway_id = await _authenticate_run_access(request, db, x_pi_run_lease)

    async def _do() -> dict[str, object]:
        run = await _leased_run(db, gateway_id, run_id, None, x_pi_run_lease)
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

    return await _with_lock_retry(db, _do)


@router.post("/runs/{run_id}/mcp/preflight")
async def mcp_preflight(
    run_id: str,
    request: Request,
    payload: PiGatewayMcpPreflightRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    x_pi_run_lease: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    gateway_id = await _authenticate_run_access(request, db, x_pi_run_lease)

    async def _do() -> dict[str, object]:
        run = await _leased_run(db, gateway_id, run_id, None, x_pi_run_lease)
        try:
            permit = await _service(db, gateway_id).preflight_mcp(run, payload)
            await db.commit()
        except TenantAccountingError as exc:
            await db.rollback()
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.code) from exc
        return permit.model_dump(mode="json")

    return await _with_lock_retry(db, _do)


@router.post("/runs/{run_id}/mcp/finalize")
async def mcp_finalize(
    run_id: str,
    request: Request,
    payload: PiGatewayMcpFinalizeRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    x_pi_run_lease: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    gateway_id = await _authenticate_run_access(request, db, x_pi_run_lease)

    async def _do() -> dict[str, object]:
        run = await _leased_run(db, gateway_id, run_id, None, x_pi_run_lease)
        try:
            result = await _service(db, gateway_id).finalize_mcp(run, payload)
            await db.commit()
        except (TenantAccountingError, ValueError) as exc:
            await db.rollback()
            code = getattr(exc, "code", str(exc))
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=code) from exc
        return result

    return await _with_lock_retry(db, _do)


@router.post("/runs/{run_id}/mcp/fail")
async def mcp_fail(
    run_id: str,
    request: Request,
    payload: PiGatewayMcpFailRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    x_pi_run_lease: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    gateway_id = await _authenticate_run_access(request, db, x_pi_run_lease)

    async def _do() -> dict[str, object]:
        run = await _leased_run(db, gateway_id, run_id, None, x_pi_run_lease)
        try:
            await _service(db, gateway_id).fail_mcp(run, payload.permit_id, payload.classification)
            await db.commit()
        except (TenantAccountingError, ValueError) as exc:
            await db.rollback()
            code = getattr(exc, "code", str(exc))
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=code) from exc
        return {"ok": True}

    return await _with_lock_retry(db, _do)


@router.post("/runs/{run_id}/terminal")
async def terminal(
    run_id: str,
    request: Request,
    payload: PiGatewayTerminalRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    x_pi_run_lease: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    # Lock Session before the Run so terminal and message/scheduler paths share
    # the Tenant → Session → Run → child lock order.  The run's session_id is
    # immutable and is only used to acquire the mutex; leased_run then performs
    # the authoritative gateway/attempt/lease validation under the Run lock.
    gateway_id = await _authenticate_run_access(request, db, x_pi_run_lease)
    outcome = RunStatus(payload.outcome)
    stream = AgentEventStream(db, request.app.state.agent_event_broker)

    async def _do() -> tuple[object, object]:
        session_id = await db.scalar(select(AgentRun.session_id).where(AgentRun.id == run_id))
        if not session_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="pi_gateway_run_not_found")
        session = await db.scalar(
            select(AgentSession)
            .where(AgentSession.id == session_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="pi_gateway_run_not_found")
        try:
            run = await _service(db, gateway_id).leased_run(run_id, payload.attempt_id, x_pi_run_lease or "")
        except PiGatewayLeaseError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="pi_gateway_run_not_found") from exc
        if outcome in (RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_WARNINGS):
            # A success terminal without a durable assistant completion is a
            # projector/worker defect: reject it; the Gateway's safe-close rule
            # then reports the Run as failed instead of completing without output.
            if not await _service(db, gateway_id).has_assistant_completion(run):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="pi_gateway_terminal_missing_completion",
                )

        async def cleanup_before_commit(locked_run) -> None:
            attempt = await db.scalar(
                select(AgentRunAttempt)
                .where(AgentRunAttempt.id == payload.attempt_id, AgentRunAttempt.run_id == locked_run.id)
                .with_for_update()
            )
            if attempt is not None and attempt.outcome == "running":
                attempt.outcome = "completed" if outcome == RunStatus.COMPLETED_WITH_WARNINGS else outcome.value
                attempt.ended_at = _service(db, gateway_id).now_fn()
            await _service(db, gateway_id).scheduler.release_run(locked_run)
            if session.active_run_id == locked_run.id:
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
                worker_id=gateway_id,
                before_commit=cleanup_before_commit,
            )
        except InvalidRunTransition as exc:
            await db.rollback()
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="pi_gateway_terminal_rejected") from exc
        return run, event

    run, event = await _with_lock_retry(db, _do)
    reconciliation_status: str | None = None
    try:
        reconciliation_status = (
            await RuntimeUsageService(db).reconcile_run(run.id)
        ).reconciliation_status
    except Exception:
        # Terminal state is already durable; a missing ledger row is an audit
        # observation failure, never a reason to reopen or mutate the Run.
        reconciliation_status = "unavailable"
    return {
        "event_id": event.id if event else None,
        "status": outcome.value,
        "reconciliation_status": reconciliation_status,
    }
