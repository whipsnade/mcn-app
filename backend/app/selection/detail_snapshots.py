from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.selection.models import KolSelectionDetailSnapshot


_SCOPE_STATUSES = {"pending", "succeeded", "failed", "skipped"}


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class DetailSnapshotStore:
    """Top20 详情快照的窄读写接口，按名单版本隔离。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def upsert(
        self,
        *,
        selection_set_id: str,
        platform: str,
        kol_uid: str,
        rank: int,
        ranking_interaction: float,
        scope_status: dict[str, str],
        facts: dict[str, Any],
        trend_points: list[dict[str, Any]],
    ) -> KolSelectionDetailSnapshot:
        if not 1 <= rank <= 20:
            raise ValueError("detail_snapshot_rank_out_of_range")
        if not platform.strip() or not kol_uid.strip():
            raise ValueError("detail_snapshot_identity_required")
        if any(status not in _SCOPE_STATUSES for status in scope_status.values()):
            raise ValueError("detail_snapshot_scope_status_invalid")

        existing = await self._db.scalar(
            select(KolSelectionDetailSnapshot)
            .where(
                KolSelectionDetailSnapshot.selection_set_id == selection_set_id,
                KolSelectionDetailSnapshot.platform == platform,
                KolSelectionDetailSnapshot.kol_uid == kol_uid,
            )
            .with_for_update()
        )
        now = _now()
        if existing is None:
            existing = KolSelectionDetailSnapshot(
                id=str(uuid4()),
                selection_set_id=selection_set_id,
                platform=platform,
                kol_uid=kol_uid,
                rank=rank,
                ranking_interaction=ranking_interaction,
                scope_status_json=dict(scope_status),
                facts_json=dict(facts),
                trend_points_json=list(trend_points),
                created_at=now,
                updated_at=now,
            )
            self._db.add(existing)
        else:
            existing.rank = rank
            existing.ranking_interaction = ranking_interaction
            existing.scope_status_json = dict(scope_status)
            existing.facts_json = dict(facts)
            existing.trend_points_json = list(trend_points)
            existing.updated_at = now
        await self._db.flush()
        return existing

    async def list_for_set(self, selection_set_id: str) -> list[KolSelectionDetailSnapshot]:
        return list(
            (
                await self._db.scalars(
                    select(KolSelectionDetailSnapshot)
                    .where(KolSelectionDetailSnapshot.selection_set_id == selection_set_id)
                    .order_by(KolSelectionDetailSnapshot.rank)
                )
            ).all()
        )
