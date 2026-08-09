import pytest
from sqlalchemy import func, select

from app.billing.models import TenantWalletTransaction
from app.billing.service import WalletService


@pytest.mark.asyncio
async def test_welcome_grant_is_idempotent(db_session, user_factory) -> None:
    user = await user_factory()
    service = WalletService(db_session)

    await service.ensure_welcome_grant(user.id)
    await service.ensure_welcome_grant(user.id)

    wallet = await service.get_wallet(user.id)
    transaction_count = await db_session.scalar(
        select(func.count(TenantWalletTransaction.id)).where(TenantWalletTransaction.user_id == user.id)
    )
    assert (wallet.balance, wallet.reserved) == (1000, 0)
    assert transaction_count == 1


@pytest.mark.asyncio
async def test_successful_and_failed_call_lifecycle(db_session, user_factory) -> None:
    user = await user_factory()
    service = WalletService(db_session)
    await service.ensure_welcome_grant(user.id)

    await service.reserve(user.id, 10, "mcp:call-1:reserve", "call-1")
    wallet = await service.get_wallet(user.id)
    assert (wallet.balance, wallet.reserved) == (990, 10)

    await service.settle(user.id, 10, "mcp:call-1:settle", "call-1")
    wallet = await service.get_wallet(user.id)
    assert (wallet.balance, wallet.reserved) == (990, 0)

    await service.reserve(user.id, 10, "mcp:call-2:reserve", "call-2")
    await service.release(user.id, 10, "mcp:call-2:release", "call-2")
    wallet = await service.get_wallet(user.id)
    assert (wallet.balance, wallet.reserved) == (990, 0)


@pytest.mark.asyncio
async def test_reserve_rejects_insufficient_balance(db_session, user_factory) -> None:
    user = await user_factory()
    service = WalletService(db_session)
    await service.ensure_welcome_grant(user.id)

    with pytest.raises(ValueError, match="mcp_cost_fixed"):
        await service.reserve(user.id, 1010, "mcp:too-large:reserve", "too-large")
