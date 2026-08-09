import pytest
from sqlalchemy import select

from app.billing.models import TenantWallet, TenantWalletTransaction, Wallet
from app.billing.service import ReservationRequest, WalletService
from app.pi_gateway.accounting import McpPreflightContext, TenantAccountingService
from app.tenancy.models import TenantMembership


@pytest.mark.asyncio
async def test_wallet_service_reads_tenant_pool_after_b4(db_session, user_factory) -> None:
    user = await user_factory()
    membership = await db_session.scalar(
        select(TenantMembership).where(TenantMembership.user_id == user.id)
    )
    assert membership is not None
    await TenantAccountingService(db_session).ensure_tenant_wallet(membership.tenant_id, balance=30)
    await TenantAccountingService(db_session).ensure_user_quota(membership.tenant_id, user.id, points_limit=100)

    wallet = await WalletService(db_session).get_wallet(user.id)
    assert (wallet.balance, wallet.reserved, wallet.available if hasattr(wallet, "available") else wallet.balance) == (30, 0, 30)


@pytest.mark.asyncio
async def test_wallet_facade_reserves_from_tenant_source_without_legacy_double_write(db_session, user_factory) -> None:
    user = await user_factory()
    membership = await db_session.scalar(
        select(TenantMembership).where(TenantMembership.user_id == user.id)
    )
    assert membership is not None
    legacy = Wallet(user_id=user.id, balance=999, reserved=0, version=0, updated_at=membership.created_at)
    db_session.add(legacy)
    await db_session.flush()
    await TenantAccountingService(db_session).ensure_tenant_wallet(membership.tenant_id, balance=20)
    await TenantAccountingService(db_session).ensure_user_quota(membership.tenant_id, user.id, points_limit=100)

    await WalletService(db_session).reserve(user.id, 10, "compat-reserve", "compat-call")
    tenant_wallet = await db_session.get(TenantWallet, membership.tenant_id)
    legacy_after = await db_session.get(Wallet, user.id)
    assert tenant_wallet is not None and (tenant_wallet.balance, tenant_wallet.reserved) == (10, 10)
    assert legacy_after is not None and (legacy_after.balance, legacy_after.reserved) == (999, 0)


@pytest.mark.asyncio
async def test_wallet_batch_facade_uses_tenant_ledger_without_legacy_rows(db_session, user_factory) -> None:
    user = await user_factory()
    membership = await db_session.scalar(
        select(TenantMembership).where(TenantMembership.user_id == user.id)
    )
    assert membership is not None
    await TenantAccountingService(db_session).ensure_tenant_wallet(membership.tenant_id, balance=30)
    await TenantAccountingService(db_session).ensure_user_quota(membership.tenant_id, user.id, points_limit=100)

    await WalletService(db_session).reserve_batch(
        user.id,
        [
            ReservationRequest("batch-call-1", "batch-reserve-1"),
            ReservationRequest("batch-call-2", "batch-reserve-2"),
        ],
    )

    tenant_rows = list(
        (
            await db_session.scalars(
                select(TenantWalletTransaction).where(
                    TenantWalletTransaction.tenant_id == membership.tenant_id,
                    TenantWalletTransaction.kind == "reserve",
                )
            )
        ).all()
    )
    assert {row.tool_call_id for row in tenant_rows} == {"batch-call-1", "batch-call-2"}
    assert await db_session.scalar(select(Wallet).where(Wallet.user_id == user.id)) is None


@pytest.mark.asyncio
async def test_wallet_available_is_pool_and_current_user_quota_intersection(
    db_session, user_factory
) -> None:
    user = await user_factory()
    membership = await db_session.scalar(
        select(TenantMembership).where(TenantMembership.user_id == user.id)
    )
    assert membership is not None
    accounting = TenantAccountingService(db_session)
    await accounting.ensure_tenant_wallet(membership.tenant_id, balance=100)
    await accounting.ensure_user_quota(membership.tenant_id, user.id, points_limit=20)

    wallet = await WalletService(db_session).get_wallet(user.id)
    assert wallet.balance == 100
    assert await WalletService(db_session).available_points(user.id) == 20

    await accounting.reserve_mcp_call(
        McpPreflightContext(
            tenant_id=membership.tenant_id,
            user_id=user.id,
            run_id="run-quota-view",
            tool_call_id="call-quota-view",
            internal_tool_name="query_analysis_data",
            service_slug="insight-cube-mcp",
            arguments={},
            feature="brand_analysis",
        )
    )
    assert await WalletService(db_session).available_points(user.id) == 10


@pytest.mark.asyncio
async def test_disabled_membership_cannot_fall_back_to_legacy_wallet_writes(
    db_session, user_factory
) -> None:
    user = await user_factory()
    membership = await db_session.scalar(
        select(TenantMembership).where(TenantMembership.user_id == user.id)
    )
    assert membership is not None
    membership.status = "disabled"
    legacy = Wallet(
        user_id=user.id,
        balance=50,
        reserved=0,
        version=0,
        updated_at=membership.created_at,
    )
    db_session.add(legacy)
    await db_session.flush()

    with pytest.raises(LookupError, match="tenant_membership_disabled"):
        await WalletService(db_session).reserve(user.id, 10, "disabled-reserve", "disabled-call")

    current = await db_session.get(Wallet, user.id)
    assert current is not None and (current.balance, current.reserved) == (50, 0)
