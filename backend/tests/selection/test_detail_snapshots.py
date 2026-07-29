from __future__ import annotations

import pytest

from app.selection.detail_snapshots import DetailSnapshotStore
from app.selection.service import KolSelectionService

from .test_selection_sets import _create_session


@pytest.mark.asyncio
async def test_detail_snapshot_is_version_scoped_and_upserts_by_kol_identity(
    db_session, user_factory
) -> None:
    user_id, session_id = await _create_session(db_session, user_factory)
    selections = KolSelectionService(db_session)
    first_set = await selections.ensure_selection_set(
        user_id, session_id, task_id="task-1", title="名单"
    )
    second_set = await selections.ensure_selection_set(
        user_id, session_id, task_id="task-2", title="名单"
    )
    store = DetailSnapshotStore(db_session)

    first = await store.upsert(
        selection_set_id=first_set.id,
        platform="douyin",
        kol_uid="dy-1",
        rank=1,
        ranking_interaction=1234.0,
        scope_status={"fansAudience": "pending", "postSummaryStatistics": "pending", "accountTrend": "pending"},
        facts={"followers": 1000},
        trend_points=[],
    )
    updated = await store.upsert(
        selection_set_id=first_set.id,
        platform="douyin",
        kol_uid="dy-1",
        rank=2,
        ranking_interaction=1500.0,
        scope_status={"fansAudience": "succeeded", "postSummaryStatistics": "pending", "accountTrend": "pending"},
        facts={"followers": 1000, "effective_follower_rate": 60.0},
        trend_points=[{"week_start": "2026-07-06", "average_interactions": 1500.0}],
    )
    other_version = await store.upsert(
        selection_set_id=second_set.id,
        platform="douyin",
        kol_uid="dy-1",
        rank=1,
        ranking_interaction=800.0,
        scope_status={"fansAudience": "pending", "postSummaryStatistics": "pending", "accountTrend": "pending"},
        facts={},
        trend_points=[],
    )

    assert updated.id == first.id
    assert updated.rank == 2
    assert updated.ranking_interaction == 1500.0
    assert updated.facts_json["effective_follower_rate"] == 60.0
    assert other_version.id != first.id
    assert len(await store.list_for_set(first_set.id)) == 1
    assert len(await store.list_for_set(second_set.id)) == 1


@pytest.mark.asyncio
async def test_detail_snapshot_rejects_rank_over_top20(db_session, user_factory) -> None:
    user_id, session_id = await _create_session(db_session, user_factory)
    selection_set = await KolSelectionService(db_session).ensure_selection_set(
        user_id, session_id, task_id="task-1", title="名单"
    )

    with pytest.raises(ValueError, match="detail_snapshot_rank_out_of_range"):
        await DetailSnapshotStore(db_session).upsert(
            selection_set_id=selection_set.id,
            platform="xiaohongshu",
            kol_uid="xhs-21",
            rank=21,
            ranking_interaction=1.0,
            scope_status={},
            facts={},
            trend_points=[],
        )
