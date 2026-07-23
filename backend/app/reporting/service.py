from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import case, func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.reporting.models import Kol, TaskCandidate, UserKolFavorite
from app.tasks.models import AnalysisTask
from app.workspace.models import WorkspaceSession


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class ReportingService:
    """收藏与最新会话任务查询；候选/BI/导出链路已随 pipeline 模式移除。"""

    def __init__(self, db_session: AsyncSession) -> None:
        self._db = db_session

    async def list_favorites(self, user_id: str) -> list[tuple[UserKolFavorite, Kol | None]]:
        return list(
            (
                await self._db.execute(
                    select(UserKolFavorite, Kol)
                    .outerjoin(Kol, Kol.id == UserKolFavorite.kol_id)
                    .where(UserKolFavorite.user_id == user_id)
                    .order_by(UserKolFavorite.created_at.desc())
                )
            ).all()
        )

    async def create_favorite(
        self, user_id: str, *, kol_id: str, note: str | None, source_task_id: str | None
    ) -> tuple[UserKolFavorite, Kol]:
        async with self._transaction():
            kol = await self._db.get(Kol, kol_id)
            if kol is None:
                raise LookupError("kol_not_found")
            if source_task_id is not None:
                await self._owned_task(user_id, source_task_id)
                candidate = await self._db.scalar(
                    select(TaskCandidate.id).where(
                        TaskCandidate.task_id == source_task_id, TaskCandidate.kol_id == kol_id
                    )
                )
                if candidate is None:
                    raise LookupError("candidate_not_found")
            now = _now()
            statement = mysql_insert(UserKolFavorite).values(
                id=str(uuid4()),
                user_id=user_id,
                kol_id=kol_id,
                note=note,
                source_task_id=source_task_id,
                created_at=now,
                updated_at=now,
            )
            await self._db.execute(
                statement.on_duplicate_key_update(
                    note=func.coalesce(statement.inserted.note, UserKolFavorite.note),
                    source_task_id=func.coalesce(
                        statement.inserted.source_task_id, UserKolFavorite.source_task_id
                    ),
                    updated_at=now,
                )
            )
            await self._db.flush()
            favorite = await self._db.scalar(
                select(UserKolFavorite).where(
                    UserKolFavorite.user_id == user_id, UserKolFavorite.kol_id == kol_id
                )
            )
            if favorite is None:
                raise RuntimeError("favorite_upsert_failed")
            return favorite, kol

    async def create_favorite_by_key(
        self,
        user_id: str,
        *,
        platform: str,
        kol_uid: str,
        nickname: str,
        snapshot: dict[str, Any] | None,
    ) -> UserKolFavorite:
        """platform+kol_uid 新路径：幂等 upsert，不走 TaskCandidate 校验。

        重复收藏时 nickname/snapshot 仅在新值非空时更新；snapshot 按顶层键合并，
        新值为 None/空串的键不覆盖旧值。
        """
        async with self._transaction():
            now = _now()
            statement = mysql_insert(UserKolFavorite).values(
                id=str(uuid4()),
                user_id=user_id,
                platform=platform,
                kol_uid=kol_uid,
                nickname=nickname,
                snapshot_json=snapshot,
                created_at=now,
                updated_at=now,
            )
            await self._db.execute(statement.on_duplicate_key_update(updated_at=now))
            await self._db.flush()
            favorite = await self._db.scalar(
                select(UserKolFavorite)
                .where(
                    UserKolFavorite.user_id == user_id,
                    UserKolFavorite.platform == platform,
                    UserKolFavorite.kol_uid == kol_uid,
                )
                .with_for_update()
            )
            if favorite is None:
                raise RuntimeError("favorite_upsert_failed")
            if nickname:
                favorite.nickname = nickname
            if snapshot:
                merged = dict(favorite.snapshot_json or {})
                for key, value in snapshot.items():
                    if value is not None and value != "":
                        merged[key] = value
                favorite.snapshot_json = merged
            favorite.updated_at = now
            await self._db.flush()
            return favorite

    async def delete_favorite_by_key(self, user_id: str, platform: str, kol_uid: str) -> None:
        async with self._transaction():
            favorite = await self._db.scalar(
                select(UserKolFavorite)
                .where(
                    UserKolFavorite.user_id == user_id,
                    UserKolFavorite.platform == platform,
                    UserKolFavorite.kol_uid == kol_uid,
                )
                .with_for_update()
            )
            if favorite is None:
                raise LookupError("favorite_not_found")
            await self._db.delete(favorite)
            await self._db.flush()

    async def delete_favorite(self, user_id: str, kol_id: str) -> None:
        async with self._transaction():
            favorite = await self._db.scalar(
                select(UserKolFavorite)
                .where(UserKolFavorite.user_id == user_id, UserKolFavorite.kol_id == kol_id)
                .with_for_update()
            )
            if favorite is None:
                raise LookupError("favorite_not_found")
            await self._db.delete(favorite)
            await self._db.flush()

    async def latest_session_analysis(
        self, user_id: str, session_id: str
    ) -> AnalysisTask | None:
        """会话的最新任务；不回退到更早任务，也不附带候选/BI 产物。"""
        return await self._db.scalar(
            select(AnalysisTask)
            .join(WorkspaceSession, WorkspaceSession.id == AnalysisTask.session_id)
            .where(
                AnalysisTask.user_id == user_id,
                AnalysisTask.session_id == session_id,
                WorkspaceSession.deleted_at.is_(None),
            )
            .order_by(*self._latest_task_order())
        )

    async def _owned_task(self, user_id: str, task_id: str) -> AnalysisTask:
        task = await self._db.scalar(
            select(AnalysisTask)
            .join(WorkspaceSession, WorkspaceSession.id == AnalysisTask.session_id)
            .where(
                AnalysisTask.id == task_id,
                AnalysisTask.user_id == user_id,
                WorkspaceSession.deleted_at.is_(None),
            )
        )
        if task is None:
            raise LookupError("task_not_found")
        return task

    @staticmethod
    def _latest_task_order() -> tuple[Any, ...]:
        return (
            case((AnalysisTask.creation_order.is_(None), 1), else_=0),
            AnalysisTask.creation_order.desc(),
            AnalysisTask.created_at.desc(),
            AnalysisTask.id.desc(),
        )

    def _transaction(self):
        return self._db.begin_nested() if self._db.in_transaction() else self._db.begin()
