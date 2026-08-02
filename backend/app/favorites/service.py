"""收藏服务：user_kol_favorites 旧表上的读/写/幂等 upsert 与圈选名单解析。

从旧 reporting.service 拆出，只依赖保留的 legacy ORM（reporting/tasks/workspace/
selection 的 models.py）。收藏继续使用 /api/v1/favorites 契约与 user_kol_favorites
旧表。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import desc, func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.reporting.models import Kol, TaskCandidate, UserKolFavorite
from app.selection.models import KolSelectionItem, KolSelectionSet, SessionKolSelection
from app.tasks.models import AnalysisTask
from app.workspace.models import WorkspaceSession


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def serialize_selection_item(
    row: SessionKolSelection | KolSelectionItem,
) -> dict[str, Any]:
    """端点 item DTO：session_kol_selections 与 kol_selection_items 行形状一致。"""
    return {
        "platform": row.platform,
        "kol_uid": row.kol_uid,
        "nickname": row.nickname,
        "followers": row.followers,
        "city": row.city,
        "profile_url": row.profile_url,
        "fields": row.fields_json,
        "score": row.score_json,
    }


class FavoritesService:
    """收藏（user_kol_favorites）的列表、创建（kol_id / platform+kol_uid）与删除。"""

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
        """旧路径：绑定 kols 表行；source_task_id 存在时校验任务归属与候选。"""
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

    async def resolve_selection_ref(
        self, user_id: str, *, platform: str, kol_uid: str
    ) -> tuple[KolSelectionSet, KolSelectionItem]:
        """解析收藏达人的最新圈选名单条目（无则 LookupError）。"""
        row = (
            await self._db.execute(
                select(KolSelectionItem, KolSelectionSet)
                .join(KolSelectionSet, KolSelectionItem.selection_set_id == KolSelectionSet.id)
                .where(
                    KolSelectionItem.user_id == user_id,
                    KolSelectionItem.platform == platform,
                    KolSelectionItem.kol_uid == kol_uid,
                )
                .order_by(desc(KolSelectionSet.created_at))
                .limit(1)
            )
        ).first()
        if row is None:
            raise LookupError("selection_ref_not_found")
        item, selection_set = row
        return selection_set, item

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

    def _transaction(self):
        return self._db.begin_nested() if self._db.in_transaction() else self._db.begin()
