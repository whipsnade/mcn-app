from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.identity.dependencies import FunctionScopedCurrentUser
from app.thinking.contracts import ThinkingEvent
from app.thinking.service import (
    SessionThinkingService,
    ThinkingQueue,
    get_session_thinking_service,
)
from app.workspace.models import WorkspaceSession


router = APIRouter()
_KEEPALIVE_SECONDS = 15.0


def encode_thinking_event(event: ThinkingEvent) -> str:
    data = json.dumps(event.payload, ensure_ascii=False, separators=(",", ":"))
    return f"id: {event.id}\nevent: {event.type}\ndata: {data}\n\n"


async def thinking_event_chunks(
    service: SessionThinkingService,
    session_id: str,
    queue: ThinkingQueue,
) -> AsyncIterator[str]:
    try:
        yield ": keepalive\n\n"
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=_KEEPALIVE_SECONDS)
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            if event is None:
                return
            yield encode_thinking_event(event)
    finally:
        await service.unsubscribe(session_id, queue)


@router.get("/{session_id}/events")
async def stream_session_thinking_events(
    session_id: str,
    user: FunctionScopedCurrentUser,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    service: Annotated[SessionThinkingService, Depends(get_session_thinking_service)],
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    del last_event_id  # 仅兼容接收；重连状态由 subscribe 生成的 snapshot 提供。
    owned_session_id = await db.scalar(
        select(WorkspaceSession.id).where(
            WorkspaceSession.id == session_id,
            WorkspaceSession.user_id == user.id,
            WorkspaceSession.deleted_at.is_(None),
        )
    )
    if owned_session_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="session_not_found",
        )
    queue = await service.subscribe(session_id)
    return StreamingResponse(
        thinking_event_chunks(service, session_id, queue),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


__all__ = [
    "encode_thinking_event",
    "router",
    "stream_session_thinking_events",
    "thinking_event_chunks",
]
