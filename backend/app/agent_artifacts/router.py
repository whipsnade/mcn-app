"""Agent Artifact 读取 API（设计 §15.2 / Task 19）。

列表 / 详情 / 版本 / 未读水位 / Excel 导出。只读已发布不可变 Version，不调用
模型/MCP（§10.1 表现层边界）。所有归属失败统一 404；导出不支持的类型或未发布
draft → 409 ``ARTIFACT_EXPORT_UNSUPPORTED``。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_artifacts.exporters import ArtifactExportUnsupported, export_artifact
from app.agent_artifacts.models import (
    AgentArtifact,
    AgentArtifactReadState,
    AgentArtifactVersion,
    ArtifactEvent,
)
from app.agent_artifacts.service import ArtifactService
from app.agent_runtime.models import AgentSession
from app.db.session import get_db
from app.identity.dependencies import CurrentUser

router = APIRouter()

_XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class AgentArtifactRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    module: str
    artifact_type: str
    parent_artifact_id: str | None = None
    artifact_key: str
    status: str
    latest_version: int
    activity_sequence: int
    created_at: datetime
    updated_at: datetime


class AgentArtifactVersionRead(BaseModel):
    id: str
    artifact_id: str
    version: int
    schema_version: str
    data_status: str
    payload: dict[str, Any] | None = None
    evidence_refs: list[dict[str, Any]] | None = None
    created_at: datetime


class ArtifactReadStateSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    module: str = Field(min_length=1, max_length=32)
    last_seen_sequence: int = Field(ge=0)


class ArtifactReadStateRead(BaseModel):
    module: str
    last_seen_sequence: int


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


async def _get_owned_artifact(
    db: AsyncSession, user_id: str, artifact_id: str
) -> AgentArtifact:
    """Artifact 归属 + 父 Session 软删除态校验（设计 §15.2「同时校验 Session 归属与软删除」）。"""
    artifact = await db.scalar(
        select(AgentArtifact)
        .join(AgentSession, AgentArtifact.session_id == AgentSession.id)
        .where(
            AgentArtifact.id == artifact_id,
            AgentArtifact.user_id == user_id,
            AgentSession.archived_at.is_(None),
        )
    )
    if artifact is None:
        raise _not_found("artifact_not_found")
    return artifact


def _version_read(version: AgentArtifactVersion) -> AgentArtifactVersionRead:
    return AgentArtifactVersionRead(
        id=version.id,
        artifact_id=version.artifact_id,
        version=version.version,
        schema_version=version.schema_version,
        data_status=version.data_status,
        payload=version.payload_json,
        evidence_refs=version.evidence_refs_json,
        created_at=version.created_at,
    )


def _sanitize_filename(name: str) -> str:
    """只保留文件名安全字符，防止 Content-Disposition 注入。"""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    return safe or "artifact"


async def _session_max_sequence(db: AsyncSession, session_id: str) -> int:
    current = await db.scalar(
        select(func.max(ArtifactEvent.sequence)).where(ArtifactEvent.session_id == session_id)
    )
    return current or 0


# --------------------------------------------------------------------------- #
# 列表 / 详情 / 版本
# --------------------------------------------------------------------------- #


@router.get("/sessions/{session_id}/artifacts", response_model=list[AgentArtifactRead])
async def list_artifacts(
    session_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    module: str | None = Query(default=None, max_length=32),
    parent_artifact_id: str | None = Query(default=None),
) -> list[AgentArtifactRead]:
    await _get_owned_session(db, user.id, session_id)
    statement = (
        select(AgentArtifact)
        .where(
            AgentArtifact.session_id == session_id,
            AgentArtifact.user_id == user.id,
        )
        .order_by(AgentArtifact.updated_at.desc(), AgentArtifact.id)
    )
    if module is not None:
        statement = statement.where(AgentArtifact.module == module)
    if parent_artifact_id is not None:
        statement = statement.where(AgentArtifact.parent_artifact_id == parent_artifact_id)
    artifacts = list((await db.scalars(statement)).all())
    return [AgentArtifactRead.model_validate(item) for item in artifacts]


@router.get("/artifacts/{artifact_id}", response_model=AgentArtifactRead)
async def get_artifact(
    artifact_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AgentArtifactRead:
    artifact = await _get_owned_artifact(db, user.id, artifact_id)
    return AgentArtifactRead.model_validate(artifact)


@router.get("/artifacts/{artifact_id}/versions/{version}", response_model=AgentArtifactVersionRead)
async def get_artifact_version(
    artifact_id: str,
    version: int,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AgentArtifactVersionRead:
    artifact = await _get_owned_artifact(db, user.id, artifact_id)
    row = await db.scalar(
        select(AgentArtifactVersion).where(
            AgentArtifactVersion.artifact_id == artifact.id,
            AgentArtifactVersion.version == version,
        )
    )
    if row is None:
        raise _not_found("artifact_version_not_found")
    return _version_read(row)


# --------------------------------------------------------------------------- #
# 未读水位
# --------------------------------------------------------------------------- #


@router.put("/sessions/{session_id}/artifact-read-state", response_model=ArtifactReadStateRead)
async def set_artifact_read_state(
    session_id: str,
    payload: ArtifactReadStateSet,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ArtifactReadStateRead:
    await _get_owned_session(db, user.id, session_id)
    # 前端只提交自己已渲染的最新 sequence：不能超过当前 Session 的 artifact sequence。
    if payload.last_seen_sequence > await _session_max_sequence(db, session_id):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="invalid_sequence")
    new_sequence = await ArtifactService(db).advance_read_state(
        user.id, session_id, payload.module, payload.last_seen_sequence
    )
    await db.commit()
    return ArtifactReadStateRead(module=payload.module, last_seen_sequence=new_sequence)


@router.get(
    "/sessions/{session_id}/artifact-read-states",
    response_model=list[ArtifactReadStateRead],
)
async def list_artifact_read_states(
    session_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[ArtifactReadStateRead]:
    """返回该会话当前用户的全部模块已读水位，供前端初始化（刷新/切换会话）。"""
    await _get_owned_session(db, user.id, session_id)
    rows = (
        await db.scalars(
            select(AgentArtifactReadState)
            .where(
                AgentArtifactReadState.user_id == user.id,
                AgentArtifactReadState.session_id == session_id,
            )
            .order_by(AgentArtifactReadState.module)
        )
    ).all()
    return [
        ArtifactReadStateRead(module=row.module, last_seen_sequence=row.last_seen_sequence)
        for row in rows
    ]


# --------------------------------------------------------------------------- #
# 导出
# --------------------------------------------------------------------------- #


@router.get("/artifacts/{artifact_id}/export")
async def export_artifact_xlsx(
    artifact_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    version: int | None = Query(default=None, ge=1),
) -> StreamingResponse:
    # 导出器在内存中构建整个 .xlsx bytes（export_artifact 返回 bytes）再以
    # StreamingResponse 单块下发；对超大 payload 会占内存，但导出是低频表现层能力。
    artifact = await _get_owned_artifact(db, user.id, artifact_id)
    if version is None:
        # 缺省保持最新版本语义
        version_row = await db.scalar(
            select(AgentArtifactVersion)
            .where(AgentArtifactVersion.artifact_id == artifact.id)
            .order_by(AgentArtifactVersion.version.desc())
            .limit(1)
        )
        if version_row is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="ARTIFACT_EXPORT_UNSUPPORTED"
            )
    else:
        # 显式版本：不存在 → 404（与 get_artifact_version 一致，不区分归属/版本缺失）
        version_row = await db.scalar(
            select(AgentArtifactVersion).where(
                AgentArtifactVersion.artifact_id == artifact.id,
                AgentArtifactVersion.version == version,
            )
        )
        if version_row is None:
            raise _not_found("artifact_version_not_found")
    try:
        content = export_artifact(version_row)
    except ArtifactExportUnsupported as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=error.code
        ) from error
    filename = f"{_sanitize_filename(artifact.artifact_type)}_v{version_row.version}.xlsx"
    return StreamingResponse(
        iter([content]),
        media_type=_XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
