from __future__ import annotations

import pytest

from app.selection.detail_views import DetailViewStore
from app.selection.service import KolSelectionService

from .test_selection_sets import _create_session


@pytest.mark.asyncio
async def test_detail_view_is_version_scoped_and_persists_only_safe_latest_five_posts(
    db_session, user_factory
) -> None:
    """缓存覆盖必须限制热帖和字段，且同达人不同名单版本不能互相覆盖。"""
    user_id, session_id = await _create_session(db_session, user_factory)
    selections = KolSelectionService(db_session)
    first_set = await selections.ensure_selection_set(
        user_id, session_id, task_id="task-1", title="名单"
    )
    second_set = await selections.ensure_selection_set(
        user_id, session_id, task_id="task-2", title="名单"
    )
    store = DetailViewStore(db_session)

    first = await store.upsert(
        selection_set_id=first_set.id,
        platform="douyin",
        kol_uid="dy-1",
        detail={
            "followers": 120_000,
            "audience_age": {"25-34": 48},
            "raw_response": {"token": "must-not-persist"},
        },
        posts=[
            {"title": f"帖子 {index}", "platform": "douyin", "url": f"https://example.com/{index}"}
            for index in range(6)
        ],
        points_cost=20,
        posts_degraded=False,
    )
    updated = await store.upsert(
        selection_set_id=first_set.id,
        platform="douyin",
        kol_uid="dy-1",
        detail={"followers": 130_000},
        posts=[],
        points_cost=30,
        posts_degraded=True,
    )
    other_version = await store.upsert(
        selection_set_id=second_set.id,
        platform="douyin",
        kol_uid="dy-1",
        detail={"followers": 88_000},
        posts=[],
        points_cost=10,
        posts_degraded=False,
    )

    assert updated.id == first.id
    assert updated.detail_json == {"followers": 130_000}
    assert updated.posts_json == []
    assert updated.points_cost == 30
    assert updated.posts_degraded is True
    assert other_version.id != updated.id
    assert await store.get(
        selection_set_id=first_set.id, platform="douyin", kol_uid="dy-1"
    ) == updated
