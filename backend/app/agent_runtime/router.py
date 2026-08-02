"""统一 Agent API：Session、Run、SSE 与 kol-details（设计 §15.1 / Task 19）。

接线层，只组合既有服务与仓储，不重新实现业务逻辑：
- Session CRUD（``agent_sessions``，user 隔离 + 软删除）；
- ``POST messages``：单事务写用户消息 + 建 ``session_analyst_v1`` Run，提交后
  ``executor.submit(run_id)``；Idempotency-Key 幂等 + 活动 Run 并发拒绝；
- Run detail / cancel / resume（仅 paused → 新 Attempt）；
- SSE 事件流（Task 4 ``AgentEventStream`` + ``sse_event_chunks``，Last-Event-ID）；
- kol-details（Task 17 ``KolDetailRunService.create``）。

所有归属失败统一 404（无存在泄露）。
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.events import AgentEventBroker, AgentEventStream
from app.agent_runtime.executor import AgentRunExecutor
from app.agent_runtime.kol_detail import KolDetailRunService
from app.agent_runtime.models import AgentMessage, AgentRun, AgentSession
from app.agent_runtime.repository import AgentRunRepository, utc_now
from app.agent_runtime.sse import sse_event_chunks
from app.agent_runtime.state import InvalidRunTransition
from app.core.config import get_settings
from app.db.session import get_db
from app.identity.dependencies import CurrentUser

router = APIRouter()

# 同一 Session 同时只允许一个活动 session_analyst_v1 Run（设计 Task 19）。
_ACTIVE_RUN_STATUSES = frozenset({"queued", "running", "reviewing"})

SESSION_ANALYST_PROFILE = "session_analyst_v1"


# --------------------------------------------------------------------------- #
# DTO
# --------------------------------------------------------------------------- #


class AgentSessionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    status: str
    created_at: datetime
    updated_at: datetime


class AgentMessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    role: str
    content: str
    sequence: int
    run_id: str | None = None
    created_at: datetime


class AgentRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    session_id: str
    parent_run_id: str | None = None
    profile_name: str
    status: str
    outcome: str | None = None
    decision_count: int
    review_count: int
    revision_count: int
    error_code: str | None = None
    started_at: datetime | None = None
    paused_at: datetime | None = None
    completed_at: datetime | None = None


class AgentSessionDetailRead(BaseModel):
    id: str
    title: str
    status: str
    created_at: datetime
    updated_at: datetime
    messages: list[AgentMessageRead] = []
    runs: list[AgentRunRead] = []


class AgentSessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=200)


class AgentSessionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=200)


class AgentMessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=20000)


class MessageRunResponse(BaseModel):
    run_id: str
    session_id: str
    message_id: str
    status: str
    reused: bool = False


class KolDetailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: str = Field(min_length=1, max_length=32)
    kol_uid: str = Field(min_length=1, max_length=64)
    selection_artifact_id: str | None = None
    selection_version: str | None = None


class KolDetailResponse(BaseModel):
    run_id: str | None = None
    artifact_id: str | None = None
    cached: bool = False
    detail: dict[str, Any] | None = None


# --------------------------------------------------------------------------- #
# 依赖
# --------------------------------------------------------------------------- #


def get_agent_executor(request: Request) -> AgentRunExecutor:
    return request.app.state.agent_executor


def get_agent_event_broker(request: Request) -> AgentEventBroker:
    return request.app.state.agent_event_broker


def get_kol_detail_service(
    db: Annotated[AsyncSession, Depends(get_db)],
    request: Request,
) -> KolDetailRunService:
    """构建绑定请求会话的 KolDetailRunService；引擎来自进程级 engine_factory。

    worker_id 与引擎构造时一致，保证 ``_start_fresh_run`` 中 claim 的租约能由
    同一引擎 transition（Test kol_detail 装配约定）。
    """
    engine = None
    factory = getattr(request.app.state, "agent_engine_factory", None)
    worker_id = "api-kol-detail"
    if factory is not None:
        engine = factory(db, worker_id=worker_id)
    return KolDetailRunService(db, engine=engine, worker_id=worker_id)


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


async def _get_owned_session(db: AsyncSession, user_id: str, session_id: str) -> AgentSession:
    session = await db.scalar(
        select(AgentSession).where(
            AgentSession.id == session_id,
            AgentSession.user_id == user_id,
            AgentSession.archived_at.is_(None),
        )
    )
    if session is None:
        raise _not_found("session_not_found")
    return session


async def _get_owned_run(db: AsyncSession, user_id: str, run_id: str) -> AgentRun:
    run = await db.scalar(
        select(AgentRun).where(
            AgentRun.id == run_id,
            AgentRun.user_id == user_id,
            AgentRun.visibility == "user",
        )
    )
    if run is None:
        raise _not_found("run_not_found")
    return run


async def _find_idempotent_run(
    db: AsyncSession, user_id: str, session_id: str, key: str
) -> AgentRun | None:
    """按 Idempotency-Key（哈希存于 Run 的 prompt_snapshot_json）查既有 Run。"""
    return await db.scalar(
        select(AgentRun).where(
            AgentRun.user_id == user_id,
            AgentRun.session_id == session_id,
            AgentRun.visibility == "user",
            AgentRun.prompt_snapshot_json["idempotency_key"].as_string() == key,
        )
    )


def _resolve_last_event_id(header_value: str | None, query_value: str | None) -> int:
    raw_value = header_value if header_value is not None else query_value
    if raw_value is None:
        return 0
    try:
        value = int(raw_value)
    except ValueError as error:
        raise HTTPException(status_code=422, detail="invalid_last_event_id") from error
    if value < 0:
        raise HTTPException(status_code=422, detail="invalid_last_event_id")
    return value


# --------------------------------------------------------------------------- #
# Session CRUD
# --------------------------------------------------------------------------- #


@router.post("/sessions", response_model=AgentSessionRead, status_code=201)
async def create_session(
    payload: AgentSessionCreate,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AgentSessionRead:
    count = await db.scalar(
        select(func.count())
        .select_from(AgentSession)
        .where(
            AgentSession.user_id == user.id,
            AgentSession.archived_at.is_(None),
            AgentSession.title.like("新会话%"),
        )
    )
    title = payload.title or f"新会话{(count or 0) + 1}"
    now = utc_now()
    session = AgentSession(
        id=str(uuid4()),
        user_id=user.id,
        title=title,
        status="active",
        created_at=now,
        updated_at=now,
    )
    db.add(session)
    await db.commit()
    return AgentSessionRead.model_validate(session)


@router.get("/sessions", response_model=list[AgentSessionRead])
async def list_sessions(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[AgentSessionRead]:
    sessions = list(
        (
            await db.scalars(
                select(AgentSession)
                .where(
                    AgentSession.user_id == user.id,
                    AgentSession.archived_at.is_(None),
                )
                .order_by(AgentSession.updated_at.desc())
            )
        ).all()
    )
    return [AgentSessionRead.model_validate(item) for item in sessions]


@router.get("/sessions/{session_id}", response_model=AgentSessionDetailRead)
async def get_session(
    session_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AgentSessionDetailRead:
    session = await _get_owned_session(db, user.id, session_id)
    messages = list(
        (
            await db.scalars(
                select(AgentMessage)
                .where(AgentMessage.session_id == session_id)
                .order_by(AgentMessage.sequence)
            )
        ).all()
    )
    runs = list(
        (
            await db.scalars(
                select(AgentRun)
                .where(
                    AgentRun.session_id == session_id,
                    AgentRun.visibility == "user",
                )
                .order_by(AgentRun.id)
            )
        ).all()
    )
    return AgentSessionDetailRead(
        id=session.id,
        title=session.title,
        status=session.status,
        created_at=session.created_at,
        updated_at=session.updated_at,
        messages=[AgentMessageRead.model_validate(item) for item in messages],
        runs=[AgentRunRead.model_validate(item) for item in runs],
    )


@router.patch("/sessions/{session_id}", response_model=AgentSessionRead)
async def update_session(
    session_id: str,
    payload: AgentSessionUpdate,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AgentSessionRead:
    session = await _get_owned_session(db, user.id, session_id)
    if payload.title is not None:
        session.title = payload.title
    session.updated_at = utc_now()
    await db.commit()
    return AgentSessionRead.model_validate(session)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    session = await _get_owned_session(db, user.id, session_id)
    session.archived_at = utc_now()
    session.updated_at = session.archived_at
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# messages → Run
# --------------------------------------------------------------------------- #


@router.post("/sessions/{session_id}/messages", response_model=MessageRunResponse, status_code=201)
async def append_message(
    session_id: str,
    payload: AgentMessageCreate,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    executor: Annotated[AgentRunExecutor, Depends(get_agent_executor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> MessageRunResponse:
    await _get_owned_session(db, user.id, session_id)
    content_hash = _content_hash(payload.content)

    # 幂等优先：同 key + 同 payload → 复用同一 Run；不同 payload → 409。
    if idempotency_key is not None:
        existing = await _find_idempotent_run(db, user.id, session_id, idempotency_key)
        if existing is not None:
            stored_hash = (existing.prompt_snapshot_json or {}).get("content_hash")
            if stored_hash != content_hash:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail="idempotency_payload_mismatch"
                )
            await db.commit()
            return MessageRunResponse(
                run_id=existing.id,
                session_id=session_id,
                message_id=existing.input_message_id or "",
                status=existing.status,
                reused=True,
            )

    # 活动并发：同一 Session 只允许一个活动 session_analyst_v1 Run。
    active = await db.scalar(
        select(AgentRun.id)
        .where(
            AgentRun.session_id == session_id,
            AgentRun.user_id == user.id,
            AgentRun.profile_name == SESSION_ANALYST_PROFILE,
            AgentRun.status.in_(tuple(_ACTIVE_RUN_STATUSES)),
        )
        .limit(1)
    )
    if active is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="active_run_in_progress")

    # 单事务：写用户消息 + 建 queued Run（executor claim 后成为 Attempt 1）。
    now = utc_now()
    max_sequence = await db.scalar(
        select(func.max(AgentMessage.sequence)).where(AgentMessage.session_id == session_id)
    )
    message = AgentMessage(
        id=str(uuid4()),
        session_id=session_id,
        run_id=None,
        role="user",
        content=payload.content,
        metadata_json=None,
        sequence=(max_sequence or 0) + 1,
        created_at=now,
    )
    db.add(message)
    await db.flush()

    run = AgentRun(
        id=str(uuid4()),
        session_id=session_id,
        user_id=user.id,
        input_message_id=message.id,
        run_kind="user",
        visibility="user",
        profile_name=SESSION_ANALYST_PROFILE,
        profile_version="v1",
        model=get_settings().tencent_plan_model,
        status="queued",
        decision_count=0,
        review_count=0,
        revision_count=0,
    )
    if idempotency_key is not None:
        run.prompt_snapshot_json = {"idempotency_key": idempotency_key, "content_hash": content_hash}
    db.add(run)
    await db.flush()
    message.run_id = run.id

    await db.commit()
    executor.submit(run.id)
    return MessageRunResponse(
        run_id=run.id,
        session_id=session_id,
        message_id=message.id,
        status="queued",
    )


# --------------------------------------------------------------------------- #
# Run 生命周期
# --------------------------------------------------------------------------- #


@router.get("/runs/{run_id}", response_model=AgentRunRead)
async def get_run(
    run_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AgentRunRead:
    run = await _get_owned_run(db, user.id, run_id)
    return AgentRunRead.model_validate(run)


@router.post("/runs/{run_id}/cancel", response_model=AgentRunRead)
async def cancel_run(
    run_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AgentRunRead:
    run = await _get_owned_run(db, user.id, run_id)
    repo = AgentRunRepository(db)
    await repo.cancel(run.id, user.id)
    await db.commit()
    return AgentRunRead.model_validate(run)


@router.post("/runs/{run_id}/resume", response_model=AgentRunRead)
async def resume_run(
    run_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    executor: Annotated[AgentRunExecutor, Depends(get_agent_executor)],
) -> AgentRunRead:
    run = await _get_owned_run(db, user.id, run_id)
    repo = AgentRunRepository(db)
    try:
        await repo.begin_attempt(run.id, resumed=True)
    except InvalidRunTransition as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="run_not_paused") from error
    await db.commit()
    executor.submit(run.id)
    return AgentRunRead.model_validate(run)


@router.get("/runs/{run_id}/events")
async def stream_run_events(
    run_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    broker: Annotated[AgentEventBroker, Depends(get_agent_event_broker)],
    last_event_id: Annotated[str | None, Query()] = None,
    last_event_id_header: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    await _get_owned_run(db, user.id, run_id)
    seen = _resolve_last_event_id(last_event_id_header, last_event_id)
    stream = AgentEventStream(db, broker)
    return StreamingResponse(
        sse_event_chunks(stream.stream(run_id, user.id, seen)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# --------------------------------------------------------------------------- #
# kol-details
# --------------------------------------------------------------------------- #


@router.post("/sessions/{session_id}/kol-details", response_model=KolDetailResponse, status_code=201)
async def create_kol_detail(
    session_id: str,
    payload: KolDetailRequest,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[KolDetailRunService, Depends(get_kol_detail_service)],
) -> KolDetailResponse:
    await _get_owned_session(db, user.id, session_id)
    summary = await service.create(
        user.id,
        session_id,
        payload.platform,
        payload.kol_uid,
        selection_artifact_id=payload.selection_artifact_id,
        selection_version=payload.selection_version,
    )
    await db.commit()
    return KolDetailResponse(
        run_id=summary.run_id,
        artifact_id=summary.artifact_id,
        cached=summary.cached,
        detail=summary.detail,
    )
