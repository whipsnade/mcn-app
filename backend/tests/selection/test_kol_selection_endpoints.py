"""kol-selection 兼容端点切读新表（kol_selection_sets/items）的契约测试。"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.selection.models import KolSelectionItem
from app.selection.service import KolSelectionService
from app.workspace.models import WorkspaceSession


async def _seed_items(
    db_session,
    user_id: str,
    session_id: str,
    specs: list[tuple[str, float]],
    *,
    title: str = "默认名单",
) -> str:
    """播种一份 selection set 与 items（不碰旧表），返回 set_id。"""
    selection_set = await KolSelectionService(db_session).ensure_selection_set(
        user_id, session_id, title=title
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    for uid, total in specs:
        db_session.add(
            KolSelectionItem(
                id=str(uuid4()),
                user_id=user_id,
                selection_set_id=selection_set.id,
                platform="xiaohongshu",
                kol_uid=uid,
                nickname=f"达人{uid}",
                followers=1000,
                city="杭州市",
                profile_url=f"https://example.com/{uid}",
                fields_json={"export_fields": {}},
                score_json={"total": total, "rating": "推荐", "stars": "★★★★", "dimensions": {}},
                source_tool="tool",
                first_task_id="t1",
                last_task_id="t1",
                created_at=now,
                updated_at=now,
            )
        )
    await db_session.flush()
    return selection_set.id


async def _session_id_of(client) -> str:
    created = await client.post("/api/v1/sessions", json={})
    assert created.status_code == 201
    return created.json()["id"]


@pytest.mark.asyncio
async def test_list_returns_items_from_latest_set_sorted_by_score(
    auth_client_factory, db_session
) -> None:
    client = await auth_client_factory("13400000071")
    session_id = await _session_id_of(client)
    session = await db_session.get(WorkspaceSession, session_id)
    await _seed_items(
        db_session, session.user_id, session_id, [("low", 50.0), ("high", 90.0)]
    )

    response = await client.get(f"/api/v1/sessions/{session_id}/kol-selection")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert [item["kol_uid"] for item in body["items"]] == ["high", "low"]
    first = body["items"][0]
    assert set(first) == {
        "platform",
        "kol_uid",
        "nickname",
        "followers",
        "city",
        "profile_url",
        "fields",
        "score",
    }
    assert first["nickname"] == "达人high"


@pytest.mark.asyncio
async def test_list_empty_session_returns_zero_not_404(auth_client_factory) -> None:
    client = await auth_client_factory("13400000072")
    session_id = await _session_id_of(client)

    response = await client.get(f"/api/v1/sessions/{session_id}/kol-selection")

    assert response.status_code == 200
    assert response.json() == {"total": 0, "items": []}


@pytest.mark.asyncio
async def test_list_foreign_session_returns_404(auth_client_factory) -> None:
    owner = await auth_client_factory("13400000073")
    other = await auth_client_factory("13400000074")
    session_id = await _session_id_of(owner)

    response = await other.get(f"/api/v1/sessions/{session_id}/kol-selection")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_list_backfilled_session_returns_items(auth_client_factory, db_session) -> None:
    """只有回填数据（历史默认名单 set，无旧表行）的会话端点正常。"""
    client = await auth_client_factory("13400000075")
    session_id = await _session_id_of(client)
    session = await db_session.get(WorkspaceSession, session_id)
    await _seed_items(
        db_session,
        session.user_id,
        session_id,
        [("uid-a", 80.0), ("uid-b", 60.0)],
        title="历史默认名单",
    )

    response = await client.get(f"/api/v1/sessions/{session_id}/kol-selection")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert [item["kol_uid"] for item in body["items"]] == ["uid-a", "uid-b"]


@pytest.mark.asyncio
async def test_export_uses_latest_set(auth_client_factory, db_session) -> None:
    client = await auth_client_factory("13400000076")
    session_id = await _session_id_of(client)
    session = await db_session.get(WorkspaceSession, session_id)
    await _seed_items(db_session, session.user_id, session_id, [("uid-a", 80.0)])

    response = await client.get(f"/api/v1/sessions/{session_id}/kol-selection/export")

    assert response.status_code == 200
    assert (
        response.headers["content-type"]
        == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    disposition = response.headers["content-disposition"]
    # filename 经 UTF-8 百分号编码，中文部分不可直接匹配，校验编码后的时间戳后缀。
    assert disposition.startswith("attachment; filename*=UTF-8''")
    assert re.search(r"_\d{8}_\d{4}\.xlsx", disposition)


@pytest.mark.asyncio
async def test_export_empty_session_returns_409(auth_client_factory) -> None:
    client = await auth_client_factory("13400000077")
    session_id = await _session_id_of(client)

    response = await client.get(f"/api/v1/sessions/{session_id}/kol-selection/export")

    assert response.status_code == 409
    assert response.json()["detail"] == "NO_KOL_SELECTION"
