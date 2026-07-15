import pytest
from sqlalchemy import func, select

from app.billing.models import Wallet, WalletTransaction
from app.billing.service import (
    InsufficientPointsError,
    ReservationRequest,
    WalletService,
)


@pytest.mark.asyncio
async def test_batch_reservation_is_all_or_nothing(db_session, user_factory) -> None:
    user = await user_factory()
    wallet_service = WalletService(db_session)
    await wallet_service.ensure_welcome_grant(user.id)
    wallet = await db_session.get(Wallet, user.id)
    assert wallet is not None
    wallet.balance = 20
    wallet.reserved = 0
    await db_session.flush()

    requests = tuple(
        ReservationRequest(
            reference_id=f"call-{index}", idempotency_key=f"mcp:call-{index}:reserve"
        )
        for index in range(3)
    )

    with pytest.raises(InsufficientPointsError):
        await wallet_service.reserve_batch(user.id, requests)

    wallet = await wallet_service.get_wallet(user.id)
    reserve_count = await db_session.scalar(
        select(func.count(WalletTransaction.id)).where(
            WalletTransaction.user_id == user.id,
            WalletTransaction.kind == "reserve",
        )
    )
    assert (wallet.balance, wallet.reserved) == (20, 0)
    assert reserve_count == 0
