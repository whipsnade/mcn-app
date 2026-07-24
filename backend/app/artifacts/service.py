"""Artifact 注册与已读状态服务（阶段二 goal/artifact 基础设施）。

Artifact 一律按 artifact_key 幂等 upsert（goal:{goal_id}:{type} /
legacy:{domain_id}:{type}），恢复重放不产生重复行。已读状态表已建，
本服务只提供最小写入面；读取端点阶段三随前端一起上。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.artifacts.models import ArtifactReadState, TaskArtifact
from app.workspace.models import WorkspaceSession


# artifact_type → 已读模块键（阶段三三 Tab 的模块归属）。
_MODULE_KEY_BY_TYPE = {
    "kol_report": "kol_analysis",
    "kol_selection_set": "kol_selection",
    "brand_report": "brand",
    "campaign_report": "campaign",
}

# artifact_type → 必须关联的目标列（二选一外键约束的业务语义）。
_REQUIRED_TARGET_BY_TYPE = {
    "kol_report": "report_id",
    "brand_report": "report_id",
    "campaign_report": "report_id",
    "kol_selection_set": "selection_set_id",
}


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def module_key_of(artifact_type: str) -> str:
    """artifact_type → module_key 映射；未知类型抛 ValueError。"""
    try:
        return _MODULE_KEY_BY_TYPE[artifact_type]
    except KeyError:
        raise ValueError(f"unknown_artifact_type:{artifact_type}") from None


class ArtifactService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def register_artifact(
        self,
        *,
        user_id: str,
        session_id: str,
        artifact_key: str,
        artifact_type: str,
        title: str,
        version: int,
        status: str = "completed",
        task_id: str | None = None,
        goal_id: str | None = None,
        report_id: str | None = None,
        selection_set_id: str | None = None,
        scope: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> TaskArtifact:
        """按 artifact_key 幂等 upsert 一条 Artifact。

        - report_id 与 selection_set_id 必须二选一，且需满足类型语义
          （kol_report 必有 report_id、kol_selection_set 必有 selection_set_id），
          违反抛 ValueError；
        - 会话归属不符抛 ``LookupError("session_not_found")``；
        - 已存在同 key 行时更新可变字段并返回，不重复建行。
        """
        if (report_id is None) == (selection_set_id is None):
            raise ValueError("artifact_requires_exactly_one_target")
        required_target = _REQUIRED_TARGET_BY_TYPE.get(artifact_type)
        if required_target == "report_id" and report_id is None:
            raise ValueError(f"{artifact_type}_requires_report_id")
        if required_target == "selection_set_id" and selection_set_id is None:
            raise ValueError(f"{artifact_type}_requires_selection_set_id")
        await self._require_owned_session(user_id, session_id)
        now = _utcnow()
        artifact = await self._db.scalar(
            select(TaskArtifact).where(TaskArtifact.artifact_key == artifact_key)
        )
        if artifact is None:
            artifact = TaskArtifact(
                id=str(uuid4()),
                session_id=session_id,
                task_id=task_id,
                goal_id=goal_id,
                artifact_key=artifact_key,
                artifact_type=artifact_type,
                title=title[:200],
                version=version,
                status=status,
                report_id=report_id,
                selection_set_id=selection_set_id,
                scope_json=scope,
                error_code=error_code,
                created_at=now,
                updated_at=now,
            )
            self._db.add(artifact)
        else:
            artifact.title = title[:200]
            artifact.version = version
            artifact.status = status
            artifact.report_id = report_id
            artifact.selection_set_id = selection_set_id
            artifact.scope_json = scope
            artifact.error_code = error_code
            if task_id is not None:
                artifact.task_id = task_id
            if goal_id is not None:
                artifact.goal_id = goal_id
            artifact.updated_at = now
        await self._db.flush()
        return artifact

    async def mark_seen(
        self, user_id: str, session_id: str, module_key: str, artifact_id: str
    ) -> None:
        """已读游标 upsert（唯一 user_id+session_id+module_key）。"""
        await self._require_owned_session(user_id, session_id)
        now = _utcnow()
        state = await self._db.scalar(
            select(ArtifactReadState).where(
                ArtifactReadState.user_id == user_id,
                ArtifactReadState.session_id == session_id,
                ArtifactReadState.module_key == module_key,
            )
        )
        if state is None:
            state = ArtifactReadState(
                id=str(uuid4()),
                user_id=user_id,
                session_id=session_id,
                module_key=module_key,
                last_seen_artifact_id=artifact_id,
                seen_at=now,
            )
            self._db.add(state)
        else:
            state.last_seen_artifact_id = artifact_id
            state.seen_at = now
        await self._db.flush()

    async def _require_owned_session(self, user_id: str, session_id: str) -> None:
        session = await self._db.get(WorkspaceSession, session_id)
        if session is None or session.user_id != user_id or session.deleted_at is not None:
            raise LookupError("session_not_found")
