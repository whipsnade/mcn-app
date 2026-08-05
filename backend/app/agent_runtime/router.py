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

from app.agent_artifacts.models import AgentArtifact, AgentArtifactVersion
from app.agent_runtime.events import AgentEventBroker, AgentEventStream
from app.agent_runtime.executor import SESSION_ANALYST_PROFILE, AgentRunExecutor
from app.agent_runtime.kol_detail import KolDetailRunService, KolDetailSelectionRefNotFound
from app.agent_runtime.models import (
    AgentMessage,
    AgentRun,
    AgentSession,
    EvidenceItem,
    MemoryEntry,
)
from app.agent_runtime.repository import AgentRunRepository, utc_now
from app.agent_runtime.sse import sse_event_chunks
from app.agent_runtime.state import InvalidRunTransition, RunStatus
from app.agent_runtime.tools.factory import load_channel_permissions
from app.agent_runtime.utility import UtilityDispatcher
from app.core.config import get_settings
from app.db.session import get_db
from app.identity.dependencies import CurrentUser

router = APIRouter()

# 同一 Session 同时只允许一个活动 session_analyst_v1 Run（设计 Task 19）。
_ACTIVE_RUN_STATUSES = frozenset({"queued", "running", "reviewing"})

# 取消语义（§5.5）：无在飞执行的状态立即迁移 cancelled 并写终态事件；
# 有在飞执行（decide/MCP/Reviewer）的状态只写 cancel_requested，由 Engine
# 在安全点收口（恰好一个 run.cancelled 终态事件）。
_IMMEDIATE_CANCEL_STATUSES = frozenset(
    {RunStatus.QUEUED, RunStatus.PAUSED, RunStatus.CLARIFICATION_REQUESTED}
)
_REQUEST_CANCEL_STATUSES = frozenset({RunStatus.RUNNING, RunStatus.REVIEWING})


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
    # ask_user 澄清等结构化 metadata（镜像 engine._handle_ask_user 的
    # {type:'clarification', question, options}），前端据此渲染澄清 chips。
    metadata: dict[str, Any] | None = None


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
    # 父 Run 只表达澄清/钻取来源，不代表复用执行状态；每条消息仍创建新 Run。
    parent_run_id: str | None = None
    # 用户确认引用的已发布 Artifact Version（§5.4 历史复用），最多 10 个。
    artifact_version_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=10)


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


def get_utility_dispatcher(request: Request) -> UtilityDispatcher | None:
    """Utility 触发器（§6.4）；窄装配未注册 app.state 时返回 None，触发跳过。"""
    return getattr(request.app.state, "agent_utility_dispatcher", None)


async def get_kol_detail_service(
    db: Annotated[AsyncSession, Depends(get_db)],
    request: Request,
    user: CurrentUser,
) -> KolDetailRunService:
    """构建绑定请求会话的 KolDetailRunService；引擎来自进程级 engine_factory。

    worker_id 与引擎构造时一致，保证 ``_start_fresh_run`` 中 claim 的租约能由
    同一引擎 transition（Test kol_detail 装配约定）。渠道权限按当前用户查询
    注入（设计 §5.1），平台型 MCP 工具只对已授权渠道可见。
    """
    engine = None
    factory = getattr(request.app.state, "agent_engine_factory", None)
    worker_id = "api-kol-detail"
    if factory is not None:
        channel_permissions = await load_channel_permissions(db, user.id)
        engine = factory(db, worker_id=worker_id, channel_permissions=channel_permissions)
    return KolDetailRunService(db, engine=engine, worker_id=worker_id)


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


async def _get_owned_session(
    db: AsyncSession, user_id: str, session_id: str, *, for_update: bool = False
) -> AgentSession:
    statement = select(AgentSession).where(
        AgentSession.id == session_id,
        AgentSession.user_id == user_id,
        AgentSession.archived_at.is_(None),
    )
    if for_update:
        statement = statement.with_for_update()
    session = await db.scalar(statement)
    if session is None:
        raise _not_found("session_not_found")
    return session


async def _get_owned_run(db: AsyncSession, user_id: str, run_id: str) -> AgentRun:
    """Run 归属 + 父 Session 软删除态校验（设计 §15.2「同时校验 Session 归属与软删除」）。"""
    run = await db.scalar(
        select(AgentRun)
        .join(AgentSession, AgentRun.session_id == AgentSession.id)
        .where(
            AgentRun.id == run_id,
            AgentRun.user_id == user_id,
            AgentRun.visibility == "user",
            AgentSession.archived_at.is_(None),
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


async def _resolve_parent_run_id(
    db: AsyncSession, user_id: str, session_id: str, requested: str | None
) -> str | None:
    """解析新 Run 的父链接。

    显式 ``parent_run_id`` 必须属本用户、同 Session 且用户可见——任何归属失败
    统一 404，不泄漏存在性；未显式给出时，回答活动 pending_question 的消息自动
    以其来源 Run 为父（澄清回答 Run 保留父链接）。父链接只表达澄清/钻取来源，
    不代表复用执行状态。
    """
    if requested is not None:
        parent = await db.scalar(
            select(AgentRun.id).where(
                AgentRun.id == requested,
                AgentRun.user_id == user_id,
                AgentRun.session_id == session_id,
                AgentRun.visibility == "user",
            )
        )
        if parent is None:
            raise _not_found("parent_run_not_found")
        return requested
    pending = await db.scalar(
        select(MemoryEntry)
        .where(
            MemoryEntry.session_id == session_id,
            MemoryEntry.memory_type == "pending_question",
            MemoryEntry.superseded_at.is_(None),
        )
        .order_by(MemoryEntry.created_at.desc())
        .limit(1)
    )
    return pending.source_run_id if pending is not None else None


async def _supersede_pending_questions(db: AsyncSession, session_id: str) -> None:
    """新用户消息到达即视为回答了悬挂问题：supersede 活动 pending_question。"""
    pendings = await db.scalars(
        select(MemoryEntry).where(
            MemoryEntry.session_id == session_id,
            MemoryEntry.memory_type == "pending_question",
            MemoryEntry.superseded_at.is_(None),
        )
    )
    now = utc_now()
    for entry in pendings:
        entry.superseded_at = now


async def _validate_artifact_version_ids(
    db: AsyncSession, user_id: str, session_id: str, version_ids: tuple[str, ...]
) -> list[str]:
    """校验引用的 Artifact Version 属本用户、同 Session 且已发布（§5.4：
    草稿、失败产物和其他用户数据不可被引用）；任何失败统一 404。按入参
    顺序去重后返回。
    """
    validated: list[str] = []
    for version_id in dict.fromkeys(version_ids):
        row = await db.scalar(
            select(AgentArtifactVersion.id)
            .join(AgentArtifact, AgentArtifactVersion.artifact_id == AgentArtifact.id)
            .where(
                AgentArtifactVersion.id == version_id,
                AgentArtifact.user_id == user_id,
                AgentArtifact.session_id == session_id,
                AgentArtifact.status == "published",
            )
        )
        if row is None:
            raise _not_found("artifact_version_not_found")
        validated.append(version_id)
    return validated


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
                # 稳定排序（§6.4）：created_at 升序 + id tie-break；前端取最后一
                # 个用户可见 Run 作为恢复锚点，不再受随机 uuid 顺序影响。
                .order_by(AgentRun.created_at, AgentRun.id)
            )
        ).all()
    )
    return AgentSessionDetailRead(
        id=session.id,
        title=session.title,
        status=session.status,
        created_at=session.created_at,
        updated_at=session.updated_at,
        messages=[
            AgentMessageRead(
                id=item.id,
                role=item.role,
                content=item.content,
                sequence=item.sequence,
                run_id=item.run_id,
                created_at=item.created_at,
                metadata=item.metadata_json,
            )
            for item in messages
        ],
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
    utility_dispatcher: Annotated[UtilityDispatcher | None, Depends(get_utility_dispatcher)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> MessageRunResponse:
    # 以「锁 Session 行」串行化同一 Session 的并发 messages：MySQL 没有部分唯一
    # 索引兜底活动 Run 约束，未加锁的 active 检查会被两个并发请求同时通过并各自
    # 建 Run（消息 sequence 也会竞态撞 uq_agent_messages_session_sequence → 500）。
    # 先 FOR UPDATE 锁住 Session 行（同 kol_detail working-head 锁），后续请求在
    # 前一个请求提交后才拿到锁，此时 active 检查能看到已提交的 Run → 409。
    await _get_owned_session(db, user.id, session_id, for_update=True)
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

    # 引用校验（建 Run 前）：父 Run 与 Artifact Version 必须属本用户、同 Session
    # 且 Version 已发布；任何归属失败统一 404，不泄漏存在性。
    parent_run_id = await _resolve_parent_run_id(db, user.id, session_id, payload.parent_run_id)
    artifact_version_ids = await _validate_artifact_version_ids(
        db, user.id, session_id, payload.artifact_version_ids
    )

    # 活动并发：同一 Session 只允许一个活动 session_analyst_v1 Run。
    # 用 FOR UPDATE（锁定读）而不是普通一致读：请求事务的 REPEATABLE-READ 快照
    # 在鉴权阶段就已建立，普通 SELECT 看不到另一个并发请求已提交的 Run；锁定读
    # 读最新已提交数据，配合上面的 Session 行锁保证「后到者在拿到锁后看到先到者
    # 已提交的 Run → 409」，从而只有一个 Run 被创建。
    active = await db.scalar(
        select(AgentRun.id)
        .where(
            AgentRun.session_id == session_id,
            AgentRun.user_id == user.id,
            AgentRun.profile_name == SESSION_ANALYST_PROFILE,
            AgentRun.status.in_(tuple(_ACTIVE_RUN_STATUSES)),
        )
        .with_for_update()
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
        parent_run_id=parent_run_id,
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
    # 引用快照：幂等键 / 父链接 / 引用的已发布 Version 随 Run 冻结，
    # 供 Context Builder 以 run_references 注入模型上下文。
    snapshot: dict[str, Any] = {}
    if idempotency_key is not None:
        snapshot["idempotency_key"] = idempotency_key
        snapshot["content_hash"] = content_hash
    if parent_run_id is not None:
        snapshot["parent_run_id"] = parent_run_id
    if artifact_version_ids:
        snapshot["artifact_version_ids"] = artifact_version_ids
    if snapshot:
        run.prompt_snapshot_json = snapshot
    db.add(run)
    await db.flush()
    message.run_id = run.id
    # 本条消息回答了悬挂澄清：supersede 活动 pending_question，避免后续
    # 消息继续被自动挂到旧澄清 Run 下。
    await _supersede_pending_questions(db, session_id)

    await db.commit()
    executor.submit(run.id)
    # §6.4：会话首条用户消息（此前会话无任何消息）提交并成功建 Run 后
    # best-effort 生成标题；重命名保护在 UtilityRunner 内（只覆盖系统默认
    # 「新会话N」标题），失败只记 warning，不影响本响应。
    if max_sequence is None and utility_dispatcher is not None:
        utility_dispatcher.schedule_session_title(session_id=session_id, user_id=user.id)
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
    broker: Annotated[AgentEventBroker, Depends(get_agent_event_broker)],
    utility_dispatcher: Annotated[UtilityDispatcher | None, Depends(get_utility_dispatcher)],
) -> AgentRunRead:
    """取消 Run（§5.5）：queued/paused/clarification 立即迁移 cancelled 并写
    恰好一个 ``run.cancelled`` 终态事件；running/reviewing 只写
    ``cancel_requested``，由 Engine 在下一个安全点（外发前 / 模型返回后 /
    Reviewer 返回后 / 循环顶）收口。终态幂等返回。
    H1：立即取消的迁移与终态事件由 settle_terminal 同一加锁事务提交。
    """
    run = await _get_owned_run(db, user.id, run_id)
    repo = AgentRunRepository(db)
    current = RunStatus(run.status)
    settled_immediately = False
    if current in _IMMEDIATE_CANCEL_STATUSES:
        await AgentEventStream(db, broker).settle_terminal(
            run.id, user.id, RunStatus.CANCELLED, {}
        )
        settled_immediately = True
    elif current in _REQUEST_CANCEL_STATUSES:
        await repo.request_cancel(run.id, user.id)
    await db.commit()
    # §6.4：立即取消不经过 executor，主分析 Run 的终态 utility（run_summary +
    # suggestions）在这里 best-effort 触发；kol_detail_v1 等辅助 Run 不触发；
    # running/reviewing 的 request_cancel 由 Engine/executor 收口时在那一侧触发。
    if (
        settled_immediately
        and utility_dispatcher is not None
        and run.profile_name == SESSION_ANALYST_PROFILE
    ):
        utility_dispatcher.schedule_run_followups(run_id=run.id)
    return AgentRunRead.model_validate(run)


@router.post("/runs/{run_id}/resume", response_model=AgentRunRead)
async def resume_run(
    run_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    executor: Annotated[AgentRunExecutor, Depends(get_agent_executor)],
) -> AgentRunRead:
    run = await _get_owned_run(db, user.id, run_id)
    # 单活动主 Run 约束（§5.5）：锁 Session 行后检查同 Session 是否已有其他
    # 活动 session_analyst_v1 Run（queued/running/reviewing），存在则 409——
    # 与 messages 并发车道同一语义，防止 paused resume 绕过限制造成双主 Run。
    await _get_owned_session(db, user.id, run.session_id, for_update=True)
    active = await db.scalar(
        select(AgentRun.id)
        .where(
            AgentRun.session_id == run.session_id,
            AgentRun.user_id == user.id,
            AgentRun.profile_name == SESSION_ANALYST_PROFILE,
            AgentRun.status.in_(tuple(_ACTIVE_RUN_STATUSES)),
            AgentRun.id != run.id,
        )
        .with_for_update()
        .limit(1)
    )
    if active is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="active_run_in_progress"
        )
    repo = AgentRunRepository(db)
    try:
        await repo.begin_attempt(run.id, resumed=True)
    except InvalidRunTransition as error:
        # 仅 paused 可恢复；已请求取消（cancel_requested）不得再启动 Attempt，需区分。
        if str(error) == "run_cancel_requested":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="run_cancel_requested"
            ) from error
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="run_not_paused") from error
    await db.commit()
    executor.submit(run.id)
    return AgentRunRead.model_validate(run)


@router.post("/runs/{run_id}/retry", response_model=MessageRunResponse, status_code=201)
async def retry_run(
    run_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    executor: Annotated[AgentRunExecutor, Depends(get_agent_executor)],
) -> MessageRunResponse:
    """重试 failed/paused Run：创建新的 user-visible Child Run（parent_run_id
    指向原 Run），把仍有效的 Evidence / 已发布 Artifact Version 引用冻结到新
    Run 的 ``prompt_snapshot_json``；绝不修改或重开原 Run。
    """
    run = await _get_owned_run(db, user.id, run_id)
    if run.status not in (RunStatus.FAILED, RunStatus.PAUSED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="run_not_retryable"
        )
    # 单活动主 Run 约束：与 messages/resume 同一车道语义，锁 Session 行后检查。
    await _get_owned_session(db, user.id, run.session_id, for_update=True)
    active = await db.scalar(
        select(AgentRun.id)
        .where(
            AgentRun.session_id == run.session_id,
            AgentRun.user_id == user.id,
            AgentRun.profile_name == SESSION_ANALYST_PROFILE,
            AgentRun.status.in_(tuple(_ACTIVE_RUN_STATUSES)),
        )
        .with_for_update()
        .limit(1)
    )
    if active is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="active_run_in_progress"
        )

    # 冻结仍有效的引用：available Evidence + 原 Run 产出且仍 published 的 Version。
    evidence_ids = list(
        (
            await db.scalars(
                select(EvidenceItem.id)
                .where(
                    EvidenceItem.run_id == run.id,
                    EvidenceItem.availability_status == "available",
                )
                .order_by(EvidenceItem.collected_at, EvidenceItem.id)
            )
        ).all()
    )
    version_ids = list(
        (
            await db.scalars(
                select(AgentArtifactVersion.id)
                .join(AgentArtifact, AgentArtifactVersion.artifact_id == AgentArtifact.id)
                .where(
                    AgentArtifactVersion.source_run_id == run.id,
                    AgentArtifact.status == "published",
                )
                .order_by(AgentArtifactVersion.created_at, AgentArtifactVersion.id)
            )
        ).all()
    )

    retried = AgentRun(
        id=str(uuid4()),
        session_id=run.session_id,
        user_id=user.id,
        input_message_id=run.input_message_id,
        parent_run_id=run.id,
        run_kind="user",
        visibility="user",
        profile_name=run.profile_name,
        profile_version=run.profile_version,
        model=get_settings().tencent_plan_model,
        status="queued",
        decision_count=0,
        review_count=0,
        revision_count=0,
        prompt_snapshot_json={
            "retry_of": run.id,
            "evidence_ids": evidence_ids,
            "artifact_version_ids": version_ids,
        },
    )
    db.add(retried)
    await db.commit()
    executor.submit(retried.id)
    return MessageRunResponse(
        run_id=retried.id,
        session_id=retried.session_id,
        message_id=run.input_message_id or "",
        status="queued",
    )


@router.get("/runs/{run_id}/events")
async def stream_run_events(
    run_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    broker: Annotated[AgentEventBroker, Depends(get_agent_event_broker)],
    last_event_id: Annotated[str | None, Query()] = None,
    last_event_id_header: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    # SSE 在流生命周期内持有该请求的 DB 连接（AgentEventStream.stream 全程轮询
    # DB + broker），连接直到 Run 到达终态事件或客户端断开才释放；不应在流开启
    # 后继续用同一 session 做其他写入。
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
    try:
        summary = await service.create(
            user.id,
            session_id,
            payload.platform,
            payload.kol_uid,
            selection_artifact_id=payload.selection_artifact_id,
            selection_version=payload.selection_version,
        )
    except KolDetailSelectionRefNotFound as error:
        # §6.4/§7：selection 引用归属校验失败统一 404，不泄漏资源存在性。
        raise _not_found("kol_selection_not_found") from error
    await db.commit()
    return KolDetailResponse(
        run_id=summary.run_id,
        artifact_id=summary.artifact_id,
        cached=summary.cached,
        detail=summary.detail,
    )
