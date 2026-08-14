"""Task 10/11 补齐：tenant wallet 调整、用户周期额度、legacy 用户写幂等。"""

import pytest
from sqlalchemy import select

from app.billing.models import TenantUserQuotaPolicy, TenantWalletTransaction


async def _make_tenant(client, slug: str) -> str:
    created = await client.post(
        "/api/v1/admin/tenants",
        headers={"Idempotency-Key": f"tenant-{slug}"},
        json={"slug": slug, "name": "钱包额度测试"},
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


@pytest.mark.asyncio
async def test_wallet_adjust_is_audited_and_idempotent(authed_client_factory, db_session) -> None:
    client, _admin = await authed_client_factory()
    tenant_id = await _make_tenant(client, "wallet-adj")
    # 管理员先放一名成员（wallet adjust 需要 membership 语义目标用户）
    member = await client.post(
        f"/api/v1/admin/tenants/{tenant_id}/users",
        headers={"Idempotency-Key": "wallet-adj-user"},
        json={"nickname": "成员", "phone": "13911112222", "role": "member"},
    )
    assert member.status_code == 201, member.text
    user_id = member.json()["id"]

    adjusted = await client.post(
        f"/api/v1/admin/tenants/{tenant_id}/wallet/adjust",
        headers={"Idempotency-Key": "wallet-adj-1"},
        json={"user_id": user_id, "delta": 500, "reason": "线下补偿"},
    )
    assert adjusted.status_code == 200, adjusted.text
    body = adjusted.json()
    assert body["balance"] == 500
    assert body["tenant_id"] == tenant_id

    replay = await client.post(
        f"/api/v1/admin/tenants/{tenant_id}/wallet/adjust",
        headers={"Idempotency-Key": "wallet-adj-1"},
        json={"user_id": user_id, "delta": 500, "reason": "线下补偿"},
    )
    assert replay.status_code == 200
    assert replay.json() == body
    rows = list(
        (
            await db_session.scalars(
                select(TenantWalletTransaction).where(
                    TenantWalletTransaction.tenant_id == tenant_id,
                    TenantWalletTransaction.kind == "admin_adjust",
                )
            )
        ).all()
    )
    assert len(rows) == 1

    conflict = await client.post(
        f"/api/v1/admin/tenants/{tenant_id}/wallet/adjust",
        headers={"Idempotency-Key": "wallet-adj-1"},
        json={"user_id": user_id, "delta": 700, "reason": "不同请求"},
    )
    assert conflict.status_code == 409

    missing_key = await client.post(
        f"/api/v1/admin/tenants/{tenant_id}/wallet/adjust",
        headers={"X-Test-Raw-Write": "1"},
        json={"user_id": user_id, "delta": 100, "reason": "无键"},
    )
    assert missing_key.status_code == 400


@pytest.mark.asyncio
async def test_user_quota_policy_read_and_idempotent_update(authed_client_factory, db_session) -> None:
    client, _admin = await authed_client_factory()
    tenant_id = await _make_tenant(client, "quota-set")
    member = await client.post(
        f"/api/v1/admin/tenants/{tenant_id}/users",
        headers={"Idempotency-Key": "quota-user"},
        json={"nickname": "成员", "phone": "13933334444", "role": "member"},
    )
    assert member.status_code == 201, member.text
    user_id = member.json()["id"]

    updated = await client.put(
        f"/api/v1/admin/tenants/{tenant_id}/quota/{user_id}",
        headers={"Idempotency-Key": "quota-1"},
        json={"points_limit": 2000},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["points_limit"] == 2000
    assert updated.json()["period"] == "monthly"

    replay = await client.put(
        f"/api/v1/admin/tenants/{tenant_id}/quota/{user_id}",
        headers={"Idempotency-Key": "quota-1"},
        json={"points_limit": 2000},
    )
    assert replay.status_code == 200
    assert replay.json() == updated.json()
    policies = list(
        (
            await db_session.scalars(
                select(TenantUserQuotaPolicy).where(
                    TenantUserQuotaPolicy.tenant_id == tenant_id,
                    TenantUserQuotaPolicy.user_id == user_id,
                )
            )
        ).all()
    )
    assert len(policies) == 1

    listed = await client.get(f"/api/v1/admin/tenants/{tenant_id}/quota")
    assert listed.status_code == 200
    assert any(item["user_id"] == user_id for item in listed.json()["items"])


@pytest.mark.asyncio
async def test_legacy_user_writes_require_and_replay_idempotency_key(authed_client_factory) -> None:
    """legacy 账号管理写操作与 Task 10 模块使用同一套持久化幂等指纹。"""
    client, _admin = await authed_client_factory()
    missing = await client.post(
        "/api/v1/admin/users",
        headers={"X-Test-Raw-Write": "1"},
        json={"nickname": "无键用户", "phone": "13955556666", "role": "user"},
    )
    assert missing.status_code == 400

    created = await client.post(
        "/api/v1/admin/users",
        headers={"Idempotency-Key": "legacy-user-1"},
        json={"nickname": "幂等用户", "phone": "13955556666", "role": "user"},
    )
    assert created.status_code == 201, created.text
    replay = await client.post(
        "/api/v1/admin/users",
        headers={"Idempotency-Key": "legacy-user-1"},
        json={"nickname": "幂等用户", "phone": "13955556666", "role": "user"},
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == created.json()["id"]

    conflict = await client.post(
        "/api/v1/admin/users",
        headers={"Idempotency-Key": "legacy-user-1"},
        json={"nickname": "另一个用户", "phone": "13977778888", "role": "user"},
    )
    assert conflict.status_code == 409


@pytest.mark.asyncio
async def test_tenant_wallet_read_projection(authed_client_factory) -> None:
    """钱包只读端点：首次调整前即可读到当前余额（确认文案的数据源）。"""
    client, _admin = await authed_client_factory()
    tenant_id = await _make_tenant(client, "wallet-read")
    read = await client.get(f"/api/v1/admin/tenants/{tenant_id}/wallet")
    assert read.status_code == 200, read.text
    assert read.json()["tenant_id"] == tenant_id
    assert read.json()["balance"] == 0
    assert read.json()["reserved"] == 0
    assert "transaction_id" not in read.json()

    missing = await client.get("/api/v1/admin/tenants/00000000-0000-0000-0000-000000000000/wallet")
    assert missing.status_code in (400, 404)
