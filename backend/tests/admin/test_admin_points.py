import pytest
from sqlalchemy import func, select

from app.admin.models import AdminAuditLog
from app.billing.models import WalletTransaction
from app.billing.service import WalletService


@pytest.mark.asyncio
async def test_adjust_points_updates_balance_and_ledger(
    authed_client_factory, user_factory, db_session
) -> None:
    admin_client, _ = await authed_client_factory()
    user = await user_factory()
    await WalletService(db_session).ensure_welcome_grant(user.id)

    credited = await admin_client.post(
        f"/api/v1/admin/users/{user.id}/points",
        json={"delta": 200, "reason": "活动补偿"},
    )
    assert credited.status_code == 200
    assert credited.json()["points"] == 1200

    debited = await admin_client.post(
        f"/api/v1/admin/users/{user.id}/points",
        json={"delta": -50, "reason": "人工扣减"},
    )
    assert debited.status_code == 200
    assert debited.json()["points"] == 1150

    kinds = list(
        (
            await db_session.scalars(
                select(WalletTransaction.kind).where(
                    WalletTransaction.user_id == user.id,
                    WalletTransaction.kind == "admin_adjust",
                )
            )
        ).all()
    )
    assert len(kinds) == 2


@pytest.mark.asyncio
async def test_adjust_points_rejects_insufficient_balance(
    authed_client_factory, user_factory
) -> None:
    admin_client, _ = await authed_client_factory()
    user = await user_factory()

    response = await admin_client.post(
        f"/api/v1/admin/users/{user.id}/points",
        json={"delta": -10, "reason": "余额不足"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "INSUFFICIENT_POINTS"

    missing = await admin_client.post(
        "/api/v1/admin/users/no-such-user/points",
        json={"delta": 10, "reason": "不存在"},
    )
    assert missing.status_code == 404
    assert missing.json()["detail"] == "USER_NOT_FOUND"


@pytest.mark.asyncio
async def test_adjust_points_idempotency_key_replays_same_transaction(
    authed_client_factory, user_factory, db_session
) -> None:
    admin_client, _ = await authed_client_factory()
    user = await user_factory()
    headers = {"Idempotency-Key": "admin-adjust-test-key-1"}

    first = await admin_client.post(
        f"/api/v1/admin/users/{user.id}/points",
        json={"delta": 100, "reason": "首次"},
        headers=headers,
    )
    replay = await admin_client.post(
        f"/api/v1/admin/users/{user.id}/points",
        json={"delta": 100, "reason": "重放"},
        headers=headers,
    )
    assert first.status_code == replay.status_code == 200
    assert first.json()["transaction_id"] == replay.json()["transaction_id"]
    assert first.json()["points"] == replay.json()["points"] == 100

    count = await db_session.scalar(
        select(func.count(WalletTransaction.id)).where(
            WalletTransaction.user_id == user.id
        )
    )
    assert count == 1


@pytest.mark.asyncio
async def test_adjust_points_writes_audit_log_with_masked_phone(
    authed_client_factory, db_session
) -> None:
    admin_client, admin = await authed_client_factory()
    created = await admin_client.post(
        "/api/v1/admin/users",
        json={"nickname": "审计对象", "phone": "13812345678", "role": "user"},
    )
    user_id = created.json()["id"]

    adjusted = await admin_client.post(
        f"/api/v1/admin/users/{user_id}/points",
        json={"delta": 66, "reason": "审计验证"},
    )
    assert adjusted.status_code == 200

    audit = await db_session.scalar(
        select(AdminAuditLog).where(
            AdminAuditLog.admin_user_id == admin.id,
            AdminAuditLog.action == "points.adjust",
            AdminAuditLog.target_id == user_id,
        )
    )
    assert audit is not None
    assert audit.target_type == "wallet"
    assert "13812345678" not in str(audit.detail_json)
    assert audit.detail_json["phone"] == "[REDACTED]"
    assert audit.detail_json["delta"] == 66
    assert audit.detail_json["balance_after"] == 66

    # 交易行的 reference_id 指向审计日志。
    transaction = await db_session.scalar(
        select(WalletTransaction).where(
            WalletTransaction.id == adjusted.json()["transaction_id"]
        )
    )
    assert transaction.reference_type == "admin_adjust"
    assert transaction.reference_id == audit.id
