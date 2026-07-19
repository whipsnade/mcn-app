import pytest


@pytest.mark.asyncio
async def test_admin_endpoints_require_token(client) -> None:
    response = await client.get("/api/v1/admin/users")
    assert response.status_code == 401
    assert response.json()["detail"] == "AUTH_EXPIRED"


@pytest.mark.asyncio
async def test_admin_endpoints_reject_non_admin(authed_client_factory) -> None:
    user_client, _ = await authed_client_factory(role="user", nickname="普通用户")

    response = await user_client.get("/api/v1/admin/users")
    assert response.status_code == 403
    assert response.json()["detail"] == "FORBIDDEN"

    write = await user_client.post(
        "/api/v1/admin/users",
        json={"nickname": "x", "phone": "13800000001", "role": "user"},
    )
    assert write.status_code == 403


@pytest.mark.asyncio
async def test_admin_can_list_users(authed_client_factory) -> None:
    admin_client, admin = await authed_client_factory()

    response = await admin_client.get("/api/v1/admin/users")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] >= 1
    assert any(item["id"] == admin.id for item in body["items"])
