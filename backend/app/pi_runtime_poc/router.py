"""Pi POC 内部 HTTP 路由（仅 POC 启用，未启用时 404 隐藏）。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.events import AgentEventBroker, AgentEventStream
from app.core.config import Settings, get_settings
from app.db.session import get_db
from app.pi_runtime_poc.auth import PiPocSettingsGuard
from app.pi_runtime_poc.schemas import (
    PiToolFailed,
    PiToolSettled,
    PiToolSettledResponse,
    PiToolStarted,
    PiToolStartedResponse,
)
from app.pi_runtime_poc.service import PiEvidenceIngestService

router = APIRouter(prefix="/runs")


def _require_poc(settings: Settings) -> None:
    try:
        PiPocSettingsGuard.assert_safe(settings)
    except RuntimeError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not_found") from None


def _bearer_token(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid_pi_poc_token")
    return auth[len("Bearer ") :].strip()


def _service(request: Request, db: AsyncSession) -> PiEvidenceIngestService:
    settings = get_settings()
    _require_poc(settings)
    broker = getattr(request.app.state, "agent_event_broker", None)
    events = AgentEventStream(db, broker=broker if broker is not None else AgentEventBroker())
    return PiEvidenceIngestService(db=db, events=events, settings=settings)


@router.post("/{run_id}/tool-calls/start", response_model=PiToolStartedResponse)
async def start_tool(
    run_id: str,
    body: PiToolStarted,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PiToolStartedResponse:
    return await _service(request, db).start_tool(
        token=_bearer_token(request), run_id=run_id, request=body
    )


@router.post("/{run_id}/tool-calls/{call_id}/settle", response_model=PiToolSettledResponse)
async def settle_tool(
    run_id: str,
    call_id: str,
    body: PiToolSettled,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PiToolSettledResponse:
    return await _service(request, db).settle_tool(
        token=_bearer_token(request), run_id=run_id, call_id=call_id, request=body
    )


@router.post("/{run_id}/tool-calls/{call_id}/fail")
async def fail_tool(
    run_id: str,
    call_id: str,
    body: PiToolFailed,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, bool]:
    await _service(request, db).fail_tool(
        token=_bearer_token(request), run_id=run_id, call_id=call_id, request=body
    )
    return {"ok": True}
