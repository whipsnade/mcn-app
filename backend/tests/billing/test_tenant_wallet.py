import pytest
from sqlalchemy import select

from app.billing.models import TenantUserQuotaUsage, TenantWallet, TenantWalletTransaction
from app.pi_gateway.accounting import (
    McpPreflightContext,
    QuotaExceededError,
    TenantAccountingError,
    TenantAccountingService,
)
from app.tenancy.models import TenantMembership


def _context(*, tenant_id: str, user_id: str, call_id: str) -> McpPreflightContext:
    return McpPreflightContext(
        tenant_id=tenant_id,
        user_id=user_id,
        run_id=f"run-{call_id}",
        tool_call_id=call_id,
        internal_tool_name="query_analysis_data",
        service_slug="insight-cube-mcp",
        arguments={"keyword": "美妆"},
        feature="brand_analysis",
    )


@pytest.mark.asyncio
async def test_tenant_wallet_is_shared_and_quota_is_reserved_atomically(db_session, user_factory) -> None:
    owner = await user_factory()
    member = await user_factory()
    owner_membership = await db_session.scalar(
        select(TenantMembership).where(TenantMembership.user_id == owner.id)
    )
    member_membership = await db_session.scalar(
        select(TenantMembership).where(TenantMembership.user_id == member.id)
    )
    assert owner_membership is not None and member_membership is not None
    member_membership.tenant_id = owner_membership.tenant_id
    await db_session.flush()

    service = TenantAccountingService(db_session)
    await service.ensure_tenant_wallet(owner_membership.tenant_id, balance=20)
    await service.ensure_user_quota(owner_membership.tenant_id, owner.id, points_limit=100)
    await service.ensure_user_quota(owner_membership.tenant_id, member.id, points_limit=100)

    first = await service.reserve_mcp_call(
        _context(tenant_id=owner_membership.tenant_id, user_id=owner.id, call_id="call-1")
    )
    second = await service.reserve_mcp_call(
        _context(tenant_id=owner_membership.tenant_id, user_id=member.id, call_id="call-2")
    )
    assert first.amount == second.amount == 10
    with pytest.raises(Exception, match="tenant_wallet_insufficient"):
        await service.reserve_mcp_call(
            _context(tenant_id=owner_membership.tenant_id, user_id=owner.id, call_id="call-3")
        )

    await service.settle_mcp_call(first.permit_id, {"mode": "mcpResult", "value": {"ok": True}})
    wallet = await db_session.get(TenantWallet, owner_membership.tenant_id)
    usage = await db_session.scalar(
        select(TenantUserQuotaUsage).where(
            TenantUserQuotaUsage.tenant_id == owner_membership.tenant_id,
            TenantUserQuotaUsage.user_id == owner.id,
        )
    )
    assert wallet is not None and (wallet.balance, wallet.reserved) == (0, 10)
    assert usage is not None and (usage.spent, usage.reserved) == (10, 0)
    rows = list(
        (
            await db_session.scalars(
                select(TenantWalletTransaction).where(
                    TenantWalletTransaction.tenant_id == owner_membership.tenant_id
                )
            )
        ).all()
    )
    assert {row.kind for row in rows} == {"reserve", "settle"}


@pytest.mark.asyncio
async def test_monthly_user_quota_blocks_before_external_dispatch(db_session, user_factory) -> None:
    user = await user_factory()
    membership = await db_session.scalar(
        select(TenantMembership).where(TenantMembership.user_id == user.id)
    )
    assert membership is not None
    service = TenantAccountingService(db_session)
    await service.ensure_tenant_wallet(membership.tenant_id, balance=100)
    await service.ensure_user_quota(membership.tenant_id, user.id, points_limit=10)
    await service.reserve_mcp_call(
        _context(tenant_id=membership.tenant_id, user_id=user.id, call_id="quota-1")
    )
    with pytest.raises(QuotaExceededError, match="tenant_user_quota_exceeded"):
        await service.reserve_mcp_call(
            _context(tenant_id=membership.tenant_id, user_id=user.id, call_id="quota-2")
        )


@pytest.mark.asyncio
async def test_reserve_idempotency_cannot_rebind_existing_permit_to_another_run_or_tool(
    db_session, user_factory
) -> None:
    user = await user_factory()
    membership = await db_session.scalar(
        select(TenantMembership).where(TenantMembership.user_id == user.id)
    )
    assert membership is not None
    service = TenantAccountingService(db_session)
    await service.ensure_tenant_wallet(membership.tenant_id, balance=20)
    await service.ensure_user_quota(membership.tenant_id, user.id, points_limit=100)
    await service.reserve_mcp_call(
        _context(tenant_id=membership.tenant_id, user_id=user.id, call_id="same-call")
    )

    conflicting = McpPreflightContext(
        tenant_id=membership.tenant_id,
        user_id=user.id,
        run_id="run-other",
        tool_call_id="same-call",
        internal_tool_name="different_tool",
        service_slug="insight-cube-mcp",
        arguments={},
        feature="brand_analysis",
    )
    with pytest.raises(TenantAccountingError, match="tenant_accounting_idempotency_conflict"):
        await service.reserve_mcp_call(conflicting)
