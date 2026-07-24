"""Artifact 读取 API：四模块 summary（最新产物 + 未读）与已读状态标记。"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.artifacts.models import ArtifactReadState, TaskArtifact
from app.artifacts.service import ArtifactService
from app.db.session import get_db
from app.identity.dependencies import CurrentUser
from app.workspace.models import WorkspaceSession


router = APIRouter()

# module_key → 该模块归集的 artifact_type。
_MODULE_ARTIFACT_TYPE = {
    "brand": "brand_report",
    "campaign": "campaign_report",
    "kol_analysis": "kol_report",
    "kol_selection": "kol_selection_set",
}

ModuleKey = Literal["brand", "campaign", "kol_analysis", "kol_selection"]


class ArtifactReadStateSet(BaseModel):
    module_key: ModuleKey
    artifact_id: str


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


async def _require_owned_session(db: AsyncSession, user_id: str, session_id: str) -> None:
    session = await db.get(WorkspaceSession, session_id)
    if session is None or session.user_id != user_id or session.deleted_at is not None:
        raise _not_found("session_not_found")


def _artifact_dict(artifact: TaskArtifact) -> dict[str, Any]:
    return {
        "artifact_id": artifact.id,
        "artifact_type": artifact.artifact_type,
        "title": artifact.title,
        "version": artifact.version,
        "scope": artifact.scope_json,
        "status": artifact.status,
        "created_at": artifact.created_at.isoformat(),
    }


@router.get("/sessions/{session_id}/artifacts/summary")
async def artifacts_summary(
    session_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """四个模块各返回最新 artifact 与未读标记（未读对照 artifact_read_states）。"""
    await _require_owned_session(db, user.id, session_id)
    artifacts = list(
        (
            await db.scalars(
                select(TaskArtifact)
                .where(TaskArtifact.session_id == session_id)
                .order_by(TaskArtifact.version.desc(), TaskArtifact.created_at.desc())
            )
        ).all()
    )
    read_states = list(
        (
            await db.scalars(
                select(ArtifactReadState).where(
                    ArtifactReadState.user_id == user.id,
                    ArtifactReadState.session_id == session_id,
                )
            )
        ).all()
    )
    seen_by_module = {state.module_key: state.last_seen_artifact_id for state in read_states}
    summary: dict[str, Any] = {}
    for module_key, artifact_type in _MODULE_ARTIFACT_TYPE.items():
        typed = [artifact for artifact in artifacts if artifact.artifact_type == artifact_type]
        latest = typed[0] if typed else None
        latest_completed = next(
            (artifact for artifact in typed if artifact.status == "completed"), None
        )
        unread = latest_completed is not None and (
            seen_by_module.get(module_key) != latest_completed.id
        )
        summary[module_key] = {
            "latest_artifact": _artifact_dict(latest) if latest is not None else None,
            "unread": unread,
        }
    return summary


@router.put("/sessions/{session_id}/artifact-read-state", status_code=204)
async def mark_artifact_seen(
    session_id: str,
    payload: ArtifactReadStateSet,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """标记模块已读到指定 artifact（artifact 必须属于该会话）。"""
    await _require_owned_session(db, user.id, session_id)
    artifact = await db.get(TaskArtifact, payload.artifact_id)
    if artifact is None or artifact.session_id != session_id:
        raise _not_found("artifact_not_found")
    await ArtifactService(db).mark_seen(
        user.id, session_id, payload.module_key, payload.artifact_id
    )
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
