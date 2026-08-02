"""收藏 API（/api/v1/favorites）：列表、创建/删除、幂等、快照、用户隔离。

Task 24 从旧 reporting 包拆到独立 favorites 包，契约与 user_kol_favorites 旧表不变。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.identity.models import AuthIdentity
from app.reporting.models import Kol, UserKolFavorite
from app.selection.models import KolSelectionItem, KolSelectionSet
from app.workspace.models import WorkspaceSession


async def _create_kol(db_session: AsyncSession, suffix: str) -> Kol:
    now = datetime.now(UTC).replace(tzinfo=None)
    kol = Kol(
        id=str(uuid4()),
        platform="bilibili",
        platform_account_id=f"favorite-test-{suffix}",
        normalized_profile_url=None,
        created_at=now,
        updated_at=now,
    )
    db_session.add(kol)
    await db_session.flush()
    return kol


async def _favorite_count(db_session: AsyncSession) -> int:
    return await db_session.scalar(select(func.count(UserKolFavorite.id))) or 0


async def _user_id_for_phone(db_session: AsyncSession, phone: str) -> str:
    identity = await db_session.scalar(
        select(AuthIdentity).where(
            AuthIdentity.provider == "sms", AuthIdentity.provider_subject == phone
        )
    )
    assert identity is not None
    return identity.user_id


async def _seed_selection_item(
    db_session: AsyncSession, *, phone: str, platform: str = "xiaohongshu", kol_uid: str = "uid-ref"
) -> tuple[str, str]:
    """ORM 直接播种一个会话 + selection set + 一条 item，返回 (session_id, set_id)。"""
    now = datetime.now(UTC).replace(tzinfo=None)
    user_id = await _user_id_for_phone(db_session, phone)
    session = WorkspaceSession(
        id=str(uuid4()),
        user_id=user_id,
        title="圈选会话",
        brand="",
        status="draft",
        platforms=[],
        target_audience="",
        last_accessed_at=now,
        created_at=now,
        updated_at=now,
    )
    db_session.add(session)
    await db_session.flush()
    selection_set = KolSelectionSet(
        id=str(uuid4()),
        session_id=session.id,
        version=1,
        title="默认名单",
        status="done",
        created_at=now,
        updated_at=now,
    )
    db_session.add(selection_set)
    await db_session.flush()
    db_session.add(
        KolSelectionItem(
            id=str(uuid4()),
            user_id=user_id,
            selection_set_id=selection_set.id,
            platform=platform,
            kol_uid=kol_uid,
            nickname="圈选达人",
            followers=1000,
            city="杭州市",
            profile_url=None,
            fields_json={"export_fields": {}},
            score_json={"total": 80.0},
            source_tool="tool",
            first_task_id="t1",
            last_task_id="t1",
            created_at=now,
            updated_at=now,
        )
    )
    await db_session.flush()
    return session.id, selection_set.id


@pytest.mark.asyncio
async def test_create_favorite_by_platform_uid(auth_client_factory, db_session) -> None:
    """新路径：platform+kol_uid 创建收藏，不落 kol_id，列表带 nickname/snapshot。"""
    client = await auth_client_factory("13500000001")

    response = await client.post(
        "/api/v1/favorites",
        json={
            "platform": "xiaohongshu",
            "kol_uid": "uid-alpha",
            "nickname": "达人甲",
            "snapshot": {"followers": 5000, "price": 500},
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["kol_id"] is None
    assert payload["platform"] == "xiaohongshu"
    assert payload["kol_uid"] == "uid-alpha"
    assert payload["nickname"] == "达人甲"
    assert payload["snapshot"] == {"followers": 5000, "price": 500}

    row = await db_session.scalar(
        select(UserKolFavorite).where(UserKolFavorite.kol_uid == "uid-alpha")
    )
    assert row is not None
    assert row.kol_id is None

    listing = await client.get("/api/v1/favorites")
    assert listing.status_code == 200
    items = listing.json()
    assert len(items) == 1
    assert items[0]["id"] == row.id
    assert items[0]["nickname"] == "达人甲"
    assert items[0]["snapshot"] == {"followers": 5000, "price": 500}


@pytest.mark.asyncio
async def test_create_favorite_by_key_is_idempotent_and_merges_snapshot(
    auth_client_factory, db_session
) -> None:
    """同 key 重复收藏仍一条；snapshot 按字段合并，新值 None 不覆盖旧值。"""
    client = await auth_client_factory("13500000002")
    body = {"platform": "douyin", "kol_uid": "uid-beta", "nickname": "达人乙"}

    first = await client.post(
        "/api/v1/favorites",
        json={**body, "snapshot": {"followers": 5000, "price": 500}},
    )
    assert first.status_code == 200

    second = await client.post(
        "/api/v1/favorites",
        json={**body, "nickname": "", "snapshot": {"followers": None, "price": 1000}},
    )
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]

    assert await _favorite_count(db_session) == 1
    row = await db_session.scalar(
        select(UserKolFavorite).where(UserKolFavorite.kol_uid == "uid-beta")
    )
    assert row is not None
    assert row.nickname == "达人乙"
    assert row.snapshot_json == {"followers": 5000, "price": 1000}


@pytest.mark.asyncio
async def test_favorite_create_requires_exactly_one_identity(auth_client_factory) -> None:
    """kol_id 与 platform+kol_uid 必居其一且不两立。"""
    client = await auth_client_factory("13500000003")

    neither = await client.post("/api/v1/favorites", json={})
    assert neither.status_code == 422

    both = await client.post(
        "/api/v1/favorites",
        json={"kol_id": str(uuid4()), "platform": "douyin", "kol_uid": "uid-x"},
    )
    assert both.status_code == 422

    partial = await client.post("/api/v1/favorites", json={"platform": "douyin"})
    assert partial.status_code == 422


@pytest.mark.asyncio
async def test_delete_favorite_by_key(auth_client_factory, db_session) -> None:
    """DELETE /favorites?platform=&kol_uid= → 204；再删 → 404。"""
    client = await auth_client_factory("13500000004")
    created = await client.post(
        "/api/v1/favorites",
        json={"platform": "xiaohongshu", "kol_uid": "uid-gamma", "nickname": "达人丙"},
    )
    assert created.status_code == 200

    deleted = await client.delete(
        "/api/v1/favorites", params={"platform": "xiaohongshu", "kol_uid": "uid-gamma"}
    )
    assert deleted.status_code == 204
    assert await _favorite_count(db_session) == 0

    again = await client.delete(
        "/api/v1/favorites", params={"platform": "xiaohongshu", "kol_uid": "uid-gamma"}
    )
    assert again.status_code == 404


@pytest.mark.asyncio
async def test_favorites_are_isolated_per_user(auth_client_factory, db_session) -> None:
    """他人的收藏不可见、不可删。"""
    owner = await auth_client_factory("13500000005")
    other = await auth_client_factory("13500000006")
    created = await owner.post(
        "/api/v1/favorites",
        json={"platform": "douyin", "kol_uid": "uid-delta", "nickname": "达人丁"},
    )
    assert created.status_code == 200

    listing = await other.get("/api/v1/favorites")
    assert listing.status_code == 200
    assert listing.json() == []

    deleted = await other.delete(
        "/api/v1/favorites", params={"platform": "douyin", "kol_uid": "uid-delta"}
    )
    assert deleted.status_code == 404
    assert await _favorite_count(db_session) == 1


@pytest.mark.asyncio
async def test_legacy_kol_id_path_still_works(auth_client_factory, db_session) -> None:
    """旧 kol_id 路径：创建、列表、按 kol_id 删除仍可用。"""
    client = await auth_client_factory("13500000007")
    kol = await _create_kol(db_session, "legacy")

    created = await client.post("/api/v1/favorites", json={"kol_id": kol.id})
    assert created.status_code == 200
    payload = created.json()
    assert payload["kol_id"] == kol.id
    assert payload["platform"] == "bilibili"
    assert payload["platform_account_id"] == kol.platform_account_id

    listing = await client.get("/api/v1/favorites")
    assert [item["kol_id"] for item in listing.json()] == [kol.id]

    deleted = await client.delete(f"/api/v1/favorites/{kol.id}")
    assert deleted.status_code == 204
    assert await _favorite_count(db_session) == 0


@pytest.mark.asyncio
async def test_favorite_kol_selection_ref_resolves_latest_selection_item(
    auth_client_factory, db_session
) -> None:
    """收藏达人能解析到其最新圈选名单条目（session_id + set_id + item）。"""
    phone = "13500000008"
    client = await auth_client_factory(phone)
    session_id, set_id = await _seed_selection_item(db_session, phone=phone)

    response = await client.get(
        "/api/v1/favorites/kol-selection-ref",
        params={"platform": "xiaohongshu", "kol_uid": "uid-ref"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["session_id"] == session_id
    assert payload["set_id"] == set_id
    assert payload["item"]["kol_uid"] == "uid-ref"
    assert payload["item"]["nickname"] == "圈选达人"


@pytest.mark.asyncio
async def test_favorite_kol_selection_ref_404_without_selection_item(auth_client_factory) -> None:
    """达人不在任何圈选名单（如快捷推荐收藏的）时 404，前端回退快捷详情。"""
    client = await auth_client_factory("13500000009")

    response = await client.get(
        "/api/v1/favorites/kol-selection-ref",
        params={"platform": "douyin", "kol_uid": "uid-absent"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "selection_ref_not_found"


@pytest.mark.asyncio
async def test_favorite_kol_selection_ref_isolated_per_user(auth_client_factory, db_session) -> None:
    """他人名单里的同名达人不可解析。"""
    await auth_client_factory("13500000010")
    other = await auth_client_factory("13500000011")
    await _seed_selection_item(db_session, phone="13500000010")

    response = await other.get(
        "/api/v1/favorites/kol-selection-ref",
        params={"platform": "xiaohongshu", "kol_uid": "uid-ref"},
    )

    assert response.status_code == 404
