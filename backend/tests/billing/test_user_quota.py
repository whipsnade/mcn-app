import pytest
from sqlalchemy import select

from app.billing.models import TenantUserQuotaUsage, TenantWallet, TenantWalletTransaction
from app.pi_gateway.accounting import McpPreflightContext, TenantAccountingService
from app.tenancy.models import TenantMembership


def _ctx(tenant_id: str, user_id: str, call_id: str) -> McpPreflightContext:
    return McpPreflightContext(
        tenant_id=tenant_id,
        user_id=user_id,
        run_id=f"run-{call_id}",
        tool_call_id=call_id,
        internal_tool_name="query_analysis_data",
        service_slug="insight-cube-mcp",
        arguments={},
        feature="brand_analysis",
    )


@pytest.mark.asyncio
async def test_unknown_keeps_wallet_and_quota_reserved(db_session, user_factory) -> None:
    user = await user_factory()
    membership = await db_session.scalar(
        select(TenantMembership).where(TenantMembership.user_id == user.id)
    )
    assert membership is not None
    service = TenantAccountingService(db_session)
    await service.ensure_tenant_wallet(membership.tenant_id, balance=20)
    await service.ensure_user_quota(membership.tenant_id, user.id, points_limit=20)
    permit = await service.reserve_mcp_call(_ctx(membership.tenant_id, user.id, "unknown-1"))

    await service.fail_mcp_call(permit.permit_id, "result_unknown")
    await service.fail_mcp_call(permit.permit_id, "result_unknown")
    wallet = await db_session.get(TenantWallet, membership.tenant_id)
    usage = await db_session.scalar(
        select(TenantUserQuotaUsage).where(
            TenantUserQuotaUsage.tenant_id == membership.tenant_id,
            TenantUserQuotaUsage.user_id == user.id,
        )
    )
    assert wallet is not None and (wallet.balance, wallet.reserved) == (10, 10)
    assert usage is not None and (usage.spent, usage.reserved) == (0, 10)
    unknown = await db_session.scalar(
        select(TenantWalletTransaction).where(
            TenantWalletTransaction.tool_call_id == "unknown-1",
            TenantWalletTransaction.kind == "unknown",
        )
    )
    assert unknown is not None and (unknown.balance_after, unknown.reserved_after) == (10, 10)


@pytest.mark.asyncio
async def test_confirmed_failure_releases_reservation_idempotently(db_session, user_factory) -> None:
    user = await user_factory()
    membership = await db_session.scalar(
        select(TenantMembership).where(TenantMembership.user_id == user.id)
    )
    assert membership is not None
    service = TenantAccountingService(db_session)
    await service.ensure_tenant_wallet(membership.tenant_id, balance=20)
    await service.ensure_user_quota(membership.tenant_id, user.id, points_limit=20)
    permit = await service.reserve_mcp_call(_ctx(membership.tenant_id, user.id, "failed-1"))

    await service.fail_mcp_call(permit.permit_id, "failed_confirmed")
    await service.fail_mcp_call(permit.permit_id, "failed_confirmed")
    wallet = await db_session.get(TenantWallet, membership.tenant_id)
    usage = await db_session.scalar(
        select(TenantUserQuotaUsage).where(
            TenantUserQuotaUsage.tenant_id == membership.tenant_id,
            TenantUserQuotaUsage.user_id == user.id,
        )
    )
    assert wallet is not None and (wallet.balance, wallet.reserved) == (20, 0)
    assert usage is not None and (usage.spent, usage.reserved) == (0, 0)


@pytest.mark.asyncio
async def test_settlement_receipt_redacts_nested_sensitive_payload(db_session, user_factory) -> None:
    user = await user_factory()
    membership = await db_session.scalar(
        select(TenantMembership).where(TenantMembership.user_id == user.id)
    )
    assert membership is not None
    service = TenantAccountingService(db_session)
    await service.ensure_tenant_wallet(membership.tenant_id, balance=20)
    await service.ensure_user_quota(membership.tenant_id, user.id, points_limit=20)
    permit = await service.reserve_mcp_call(_ctx(membership.tenant_id, user.id, "safe-1"))

    receipt = await service.settle_mcp_call(
        permit.permit_id,
        {"result": {"ok": True, "nested": {"token": "do-not-store"}}},
    )

    assert receipt.payload == {"result": {"ok": True, "nested": {}}}
