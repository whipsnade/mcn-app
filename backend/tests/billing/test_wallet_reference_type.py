import pytest
from sqlalchemy import select

from app.billing.models import WalletTransaction
from app.billing.service import WalletService


@pytest.mark.asyncio
async def test_reference_type_defaults_to_mcp_call(db_session, user_factory) -> None:
    user = await user_factory()
    service = WalletService(db_session)
    await service.ensure_welcome_grant(user.id)

    await service.reserve(user.id, 10, "rt:1:reserve", "ref-1")
    await service.settle(user.id, 10, "rt:1:settle", "ref-1")

    rows = list(
        (
            await db_session.scalars(
                select(WalletTransaction).where(
                    WalletTransaction.idempotency_key.in_(["rt:1:reserve", "rt:1:settle"])
                )
            )
        ).all()
    )
    assert {tx.reference_type for tx in rows} == {"mcp_call"}


@pytest.mark.asyncio
async def test_reference_type_is_recorded_when_overridden(db_session, user_factory) -> None:
    user = await user_factory()
    service = WalletService(db_session)
    await service.ensure_welcome_grant(user.id)

    await service.reserve(
        user.id, 10, "rt:2:reserve", "ref-2", reference_type="quick_mcp_call"
    )
    await service.release(
        user.id, 10, "rt:2:release", "ref-2", reference_type="quick_mcp_call"
    )

    rows = list(
        (
            await db_session.scalars(
                select(WalletTransaction).where(
                    WalletTransaction.idempotency_key.in_(["rt:2:reserve", "rt:2:release"])
                )
            )
        ).all()
    )
    assert {tx.reference_type for tx in rows} == {"quick_mcp_call"}
    wallet = await service.get_wallet(user.id)
    assert wallet.balance == 1000
    assert wallet.reserved == 0
