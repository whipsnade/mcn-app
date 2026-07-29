from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.selection.detail_views import DetailViewStore
from app.selection.models import KolSelectionItem
from app.selection.service import KolSelectionService


_DETAIL_TOOL = "datatap.social.grow.kol.detail.v1"
_POSTS_TOOL = "datatap.insight.query.raw.posts.v1"


async def _session_id_of(client) -> str:
    response = await client.post("/api/v1/sessions", json={})
    assert response.status_code == 201
    return response.json()["id"]


async def _seed_item(db_session, user_id: str, session_id: str) -> str:
    selection_set = await KolSelectionService(db_session).ensure_selection_set(
        user_id, session_id, task_id="task-1", title="名单"
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    db_session.add(
        KolSelectionItem(
            id=str(uuid4()),
            user_id=user_id,
            selection_set_id=selection_set.id,
            platform="xiaohongshu",
            kol_uid="xhs-1",
            nickname="美食小达人",
            followers=120_000,
            city="上海市",
            profile_url="https://example.com/profile/xhs-1",
            fields_json={"export_fields": {}},
            score_json={"version": "kol_score_v2", "total": 85, "dimensions": {}},
            source_tool="tool",
            first_task_id="task-1",
            last_task_id="task-1",
            created_at=now,
            updated_at=now,
        )
    )
    await db_session.flush()
    return selection_set.id


@pytest.mark.asyncio
async def test_cached_selection_detail_returns_zero_points_without_mcp_call(
    quick_client_factory, db_session
) -> None:
    client, user, transport, _model = await quick_client_factory()
    session_id = await _session_id_of(client)
    set_id = await _seed_item(db_session, user.id, session_id)
    await DetailViewStore(db_session).upsert(
        selection_set_id=set_id,
        platform="xiaohongshu",
        kol_uid="xhs-1",
        detail={"followers": 120_000, "profile_url": "https://example.com/profile/xhs-1"},
        posts=[{"title": "缓存热帖", "platform": "xiaohongshu"}],
        points_cost=20,
        posts_degraded=False,
    )

    response = await client.get(
        f"/api/v1/sessions/{session_id}/kol-selection/detail",
        params={"set_id": set_id, "platform": "xiaohongshu", "kol_uid": "xhs-1"},
    )

    assert response.status_code == 200
    assert response.json()["source"] == "cache"
    assert response.json()["points_cost"] == 0
    assert response.json()["posts"] == [{"title": "缓存热帖", "platform": "xiaohongshu"}]
    assert transport.calls == []


@pytest.mark.asyncio
async def test_detail_cache_miss_queries_mcp_once_then_serves_cached_result(
    quick_client_factory, db_session
) -> None:
    decisions = [
        {
            "action": "call_tool",
            "internal_tool_name": _DETAIL_TOOL,
            "arguments": {
                "platform": "xiaohongshu",
                "kwUidList": ["xhs-1"],
                "scope": ["fansAudience", "postSummaryStatistics", "priceTrend"],
            },
        },
        {
            "action": "call_tool",
            "internal_tool_name": _POSTS_TOOL,
            "arguments": {
                "target_type": "field",
                "field_name": "用户昵称",
                "field_value": ["美食小达人"],
                "datasource": ["小红书"],
                "start_time": "2026-06-21",
                "end_time": "2026-07-21",
                "order_by": "互动数",
                "size": 10,
            },
        },
        {
            "action": "finish",
            "result": {
                "detail": {"账号ID (kwUid)": "xhs-1", "粉丝数": 130000},
                "posts": [
                    {"标题": f"热帖{index}", "帖子链接": f"https://example.com/{index}"}
                    for index in range(6)
                ],
            },
        },
    ]
    client, user, transport, _model = await quick_client_factory(decisions=decisions)
    transport.results["kol_detail"] = {"详情列表": [{"账号ID (kwUid)": "xhs-1", "粉丝数": 130000}]}
    transport.results["query_raw_posts"] = {"帖子列表": [{"标题": "热帖0", "帖子链接": "https://example.com/0"}]}
    session_id = await _session_id_of(client)
    set_id = await _seed_item(db_session, user.id, session_id)

    queried = await client.post(
        f"/api/v1/sessions/{session_id}/kol-selection/detail/query",
        json={"set_id": set_id, "platform": "xiaohongshu", "kol_uid": "xhs-1", "refresh": False},
    )
    cached = await client.post(
        f"/api/v1/sessions/{session_id}/kol-selection/detail/query",
        json={"set_id": set_id, "platform": "xiaohongshu", "kol_uid": "xhs-1", "refresh": False},
    )

    assert queried.status_code == 200
    assert queried.json()["source"] == "query"
    assert queried.json()["points_cost"] == 20
    assert queried.json()["detail"]["followers"] == 130000
    assert len(queried.json()["posts"]) == 5
    assert cached.status_code == 200
    assert cached.json()["source"] == "cache"
    assert cached.json()["points_cost"] == 0
    assert transport.call_count("kol_detail") == 1
    assert transport.call_count("query_raw_posts") == 1
