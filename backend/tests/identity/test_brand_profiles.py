from __future__ import annotations

import pytest
from sqlalchemy import select

from app.identity.brand_profiles import BrandProfileService
from app.identity.models import UserBrandProfile


@pytest.mark.asyncio
async def test_set_default_brand_creates_and_switches_default(db_session, user_factory) -> None:
    user = await user_factory()
    service = BrandProfileService(db_session)

    first = await service.set_default_brand(user.id, "海底捞")
    assert first.brand_name == "海底捞"
    assert first.is_default is True

    second = await service.set_default_brand(user.id, "喜茶")
    assert second.is_default is True

    rows = list(
        (
            await db_session.scalars(
                select(UserBrandProfile).where(UserBrandProfile.user_id == user.id)
            )
        ).all()
    )
    assert len(rows) == 2
    by_name = {row.brand_name: row for row in rows}
    # 换默认：旧默认被清为 NULL（唯一 (user_id, is_default) 靠 NULL 语义放行）。
    assert by_name["海底捞"].is_default is None
    assert by_name["喜茶"].is_default is True


@pytest.mark.asyncio
async def test_set_default_brand_same_brand_is_idempotent(db_session, user_factory) -> None:
    user = await user_factory()
    service = BrandProfileService(db_session)

    first = await service.set_default_brand(user.id, "海底捞")
    again = await service.set_default_brand(user.id, "海底捞")

    assert again.id == first.id
    rows = list(
        (
            await db_session.scalars(
                select(UserBrandProfile).where(UserBrandProfile.user_id == user.id)
            )
        ).all()
    )
    assert len(rows) == 1
    assert rows[0].is_default is True


@pytest.mark.asyncio
async def test_set_default_brand_strips_and_rejects_blank(db_session, user_factory) -> None:
    user = await user_factory()
    service = BrandProfileService(db_session)

    profile = await service.set_default_brand(user.id, "  海底捞  ")
    assert profile.brand_name == "海底捞"
    with pytest.raises(ValueError):
        await service.set_default_brand(user.id, "   ")


@pytest.mark.asyncio
async def test_list_and_get_default_brand(db_session, user_factory) -> None:
    user = await user_factory()
    service = BrandProfileService(db_session)

    assert await service.list_brand_profiles(user.id) == []
    assert await service.get_default_brand(user.id) is None

    await service.set_default_brand(user.id, "海底捞")
    await service.set_default_brand(user.id, "喜茶")

    names = sorted(row.brand_name for row in await service.list_brand_profiles(user.id))
    assert names == ["喜茶", "海底捞"]
    assert await service.get_default_brand(user.id) == "喜茶"


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_brand_profiles_get_empty_list(auth_client_factory) -> None:
    client = await auth_client_factory("13400000061")

    response = await client.get("/api/v1/users/me/brand-profiles")

    assert response.status_code == 200
    assert response.json() == {"items": []}


@pytest.mark.asyncio
async def test_brand_profiles_put_sets_default_and_get_reflects(auth_client_factory) -> None:
    client = await auth_client_factory("13400000062")

    put = await client.put(
        "/api/v1/users/me/brand-profiles", json={"brand_name": "海底捞"}
    )
    assert put.status_code == 200
    assert put.json() == {"items": [{"brand_name": "海底捞", "is_default": True}]}

    await client.put("/api/v1/users/me/brand-profiles", json={"brand_name": "喜茶"})
    response = await client.get("/api/v1/users/me/brand-profiles")
    assert response.status_code == 200
    items = {item["brand_name"]: item["is_default"] for item in response.json()["items"]}
    assert items == {"海底捞": False, "喜茶": True}


@pytest.mark.asyncio
async def test_brand_profiles_put_blank_name_returns_422(auth_client_factory) -> None:
    client = await auth_client_factory("13400000063")

    response = await client.put(
        "/api/v1/users/me/brand-profiles", json={"brand_name": "   "}
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_brand_profiles_requires_auth(client) -> None:
    get_response = await client.get("/api/v1/users/me/brand-profiles")
    put_response = await client.put(
        "/api/v1/users/me/brand-profiles", json={"brand_name": "海底捞"}
    )

    assert get_response.status_code == 401
    assert put_response.status_code == 401


@pytest.mark.asyncio
async def test_brand_profiles_are_isolated_per_user(auth_client_factory) -> None:
    owner = await auth_client_factory("13400000064")
    other = await auth_client_factory("13400000065")

    await owner.put("/api/v1/users/me/brand-profiles", json={"brand_name": "海底捞"})

    response = await other.get("/api/v1/users/me/brand-profiles")
    assert response.status_code == 200
    assert response.json() == {"items": []}
