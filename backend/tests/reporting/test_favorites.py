import pytest
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.reporting.models import Kol, UserKolFavorite


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
