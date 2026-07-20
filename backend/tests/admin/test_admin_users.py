import pytest
from sqlalchemy import select

from app.admin.models import AdminAuditLog
from app.billing.models import WalletTransaction
from app.identity.models import AuthIdentity, User


@pytest.mark.asyncio
async def test_create_user_with_points_and_channels(authed_client_factory, db_session) -> None:
    admin_client, _ = await authed_client_factory()

    created = await admin_client.post(
        "/api/v1/admin/users",
        json={
            "nickname": "新账号",
            "phone": "13800000002",
            "role": "user",
            "points": 300,
            "channels": ["douyin", "xiaohongshu"],
        },
    )
    assert created.status_code == 201
    item = created.json()
    assert item["nickname"] == "新账号"
    assert item["phone"] == "13800000002"
    assert item["points"] == 300
    assert item["reserved_points"] == 0
    assert set(item["channels"]) == {"douyin", "xiaohongshu"}

    ledger = await db_session.scalar(
        select(WalletTransaction).where(
            WalletTransaction.user_id == item["id"],
            WalletTransaction.kind == "admin_adjust",
        )
    )
    assert ledger is not None
    assert ledger.balance_delta == 300


@pytest.mark.asyncio
async def test_create_user_rejects_duplicate_phone(authed_client_factory) -> None:
    admin_client, _ = await authed_client_factory()
    payload = {"nickname": "账号A", "phone": "13800000003", "role": "user"}

    first = await admin_client.post("/api/v1/admin/users", json=payload)
    second = await admin_client.post(
        "/api/v1/admin/users", json={**payload, "nickname": "账号B"}
    )
    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["detail"] == "PHONE_CONFLICT"


@pytest.mark.asyncio
async def test_list_users_keyword_channel_and_pagination(authed_client_factory) -> None:
    admin_client, _ = await authed_client_factory()
    await admin_client.post(
        "/api/v1/admin/users",
        json={
            "nickname": "渠道达人",
            "phone": "13800000004",
            "role": "user",
            "channels": ["douyin"],
        },
    )
    await admin_client.post(
        "/api/v1/admin/users",
        json={
            "nickname": "另一账号",
            "phone": "13900000005",
            "role": "user",
            "channels": ["weibo"],
        },
    )

    by_keyword = await admin_client.get("/api/v1/admin/users", params={"keyword": "渠道达人"})
    assert by_keyword.status_code == 200
    assert [item["nickname"] for item in by_keyword.json()["items"]] == ["渠道达人"]

    by_phone = await admin_client.get("/api/v1/admin/users", params={"keyword": "13900000005"})
    assert [item["nickname"] for item in by_phone.json()["items"]] == ["另一账号"]

    by_channel = await admin_client.get(
        "/api/v1/admin/users", params={"channel": "douyin", "limit": 200}
    )
    channel_names = {item["nickname"] for item in by_channel.json()["items"]}
    assert "渠道达人" in channel_names
    assert "另一账号" not in channel_names

    page = await admin_client.get("/api/v1/admin/users", params={"limit": 1, "offset": 0})
    assert len(page.json()["items"]) == 1
    assert page.json()["total"] >= 3


@pytest.mark.asyncio
async def test_patch_user_role_status_channels_and_phone(authed_client_factory) -> None:
    admin_client, _ = await authed_client_factory()
    created = await admin_client.post(
        "/api/v1/admin/users",
        json={"nickname": "待编辑", "phone": "13800000006", "role": "user"},
    )
    user_id = created.json()["id"]

    updated = await admin_client.patch(
        f"/api/v1/admin/users/{user_id}",
        json={"role": "admin", "channels": ["bilibili"], "phone": "13800000007"},
    )
    assert updated.status_code == 200
    item = updated.json()
    assert item["role"] == "admin"
    assert item["channels"] == ["bilibili"]
    assert item["phone"] == "13800000007"

    disabled = await admin_client.patch(
        f"/api/v1/admin/users/{user_id}", json={"status": "disabled"}
    )
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"

    missing = await admin_client.patch(
        "/api/v1/admin/users/no-such-user", json={"nickname": "x"}
    )
    assert missing.status_code == 404
    assert missing.json()["detail"] == "USER_NOT_FOUND"


@pytest.mark.asyncio
async def test_patch_phone_conflict_returns_409(authed_client_factory) -> None:
    admin_client, _ = await authed_client_factory()
    first = await admin_client.post(
        "/api/v1/admin/users",
        json={"nickname": "账号1", "phone": "13800000008", "role": "user"},
    )
    second = await admin_client.post(
        "/api/v1/admin/users",
        json={"nickname": "账号2", "phone": "13800000009", "role": "user"},
    )

    conflict = await admin_client.patch(
        f"/api/v1/admin/users/{second.json()['id']}", json={"phone": "13800000008"}
    )
    assert first.status_code == 201
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "PHONE_CONFLICT"


@pytest.mark.asyncio
async def test_admin_cannot_demote_disable_or_delete_self(authed_client_factory) -> None:
    admin_client, admin = await authed_client_factory()

    demote = await admin_client.patch(
        f"/api/v1/admin/users/{admin.id}", json={"role": "user"}
    )
    disable = await admin_client.patch(
        f"/api/v1/admin/users/{admin.id}", json={"status": "disabled"}
    )
    delete = await admin_client.delete(f"/api/v1/admin/users/{admin.id}")
    for response in (demote, disable, delete):
        assert response.status_code == 400
        assert response.json()["detail"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_patch_user_industries(authed_client_factory, db_session) -> None:
    admin_client, _ = await authed_client_factory()
    created = await admin_client.post(
        "/api/v1/admin/users",
        json={"nickname": "行业账号", "phone": "13800000011", "role": "user"},
    )
    assert created.status_code == 201
    user_id = created.json()["id"]
    assert created.json()["industries"] == ["美食"]  # 默认行业

    updated = await admin_client.patch(
        f"/api/v1/admin/users/{user_id}",
        json={"industries": ["美妆", "母婴"]},
    )
    assert updated.status_code == 200
    assert updated.json()["industries"] == ["美妆", "母婴"]

    # 审计 detail 同步 before/after 行业。
    audit = await db_session.scalar(
        select(AdminAuditLog).where(
            AdminAuditLog.action == "user.update",
            AdminAuditLog.target_id == user_id,
        )
    )
    assert audit is not None
    assert audit.detail_json["before"]["industries"] == ["美食"]
    assert audit.detail_json["after"]["industries"] == ["美妆", "母婴"]

    # 校验：单项超 20 字 / 超过 5 项均 422。
    too_long = await admin_client.patch(
        f"/api/v1/admin/users/{user_id}", json={"industries": ["x" * 21]}
    )
    too_many = await admin_client.patch(
        f"/api/v1/admin/users/{user_id}",
        json={"industries": ["a", "b", "c", "d", "e", "f"]},
    )
    assert too_long.status_code == 422
    assert too_many.status_code == 422

    listed = await admin_client.get("/api/v1/admin/users", params={"keyword": "行业账号"})
    [item] = listed.json()["items"]
    assert item["industries"] == ["美妆", "母婴"]


@pytest.mark.asyncio
async def test_delete_user_revokes_refresh_sessions(
    authed_client_factory, auth_client_factory, db_session
) -> None:
    admin_client, _ = await authed_client_factory()
    victim_client = await auth_client_factory("13800000010")
    victim = await db_session.scalar(
        select(User).where(
            User.id.in_(
                select(AuthIdentity.user_id).where(
                    AuthIdentity.provider == "sms",
                    AuthIdentity.provider_subject == "13800000010",
                )
            )
        )
    )
    assert victim is not None

    deleted = await admin_client.delete(f"/api/v1/admin/users/{victim.id}")
    assert deleted.status_code == 204

    refresh = await victim_client.post("/api/v1/auth/refresh")
    assert refresh.status_code == 401

    # 被禁用用户的访问令牌也不再有效。
    me = await victim_client.get("/api/v1/users/me")
    assert me.status_code == 401
