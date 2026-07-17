import pytest


ALL_MOCK_CHANNELS = {"xiaohongshu", "douyin", "bilibili", "weibo", "wechat"}


@pytest.mark.asyncio
async def test_new_sms_user_can_refresh_and_receives_1000_points(client) -> None:
    code_response = await client.post(
        "/api/v1/auth/mock/sms/code", json={"phone": "13812345678"}
    )
    assert code_response.status_code == 200
    assert code_response.json()["mock_code"] == "000000"

    login = await client.post(
        "/api/v1/auth/mock/sms/login",
        json={"phone": "13812345678", "code": "000000"},
    )
    assert login.status_code == 200
    token = login.json()["access_token"]
    assert "kol_refresh" in login.cookies

    headers = {"Authorization": f"Bearer {token}"}
    me = await client.get("/api/v1/users/me", headers=headers)
    wallet = await client.get("/api/v1/wallet", headers=headers)
    assert me.status_code == 200
    assert me.json()["nickname"] == "手机用户_5678"
    assert set(me.json()["channels"]) == ALL_MOCK_CHANNELS
    assert wallet.json() == {"balance": 1000, "reserved": 0, "available": 1000}

    refreshed = await client.post("/api/v1/auth/refresh")
    assert refreshed.status_code == 200
    assert refreshed.json()["access_token"] != token


@pytest.mark.asyncio
async def test_repeat_login_does_not_repeat_welcome_grant(client) -> None:
    payload = {"phone": "13900001111", "code": "000000"}
    first = await client.post("/api/v1/auth/mock/sms/login", json=payload)
    second = await client.post("/api/v1/auth/mock/sms/login", json=payload)

    token = second.json()["access_token"]
    wallet = await client.get(
        "/api/v1/wallet", headers={"Authorization": f"Bearer {token}"}
    )
    assert first.status_code == second.status_code == 200
    assert wallet.json()["balance"] == 1000

    me = await client.get(
        "/api/v1/users/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert set(me.json()["channels"]) == ALL_MOCK_CHANNELS


@pytest.mark.asyncio
async def test_logout_revokes_refresh_session(client) -> None:
    login = await client.post(
        "/api/v1/auth/mock/wechat/login",
        json={"mock_ticket": "mock-wechat-authorized"},
    )
    assert login.status_code == 200

    logout = await client.post("/api/v1/auth/logout")
    refresh = await client.post("/api/v1/auth/refresh")
    assert logout.status_code == 204
    assert refresh.status_code == 401
