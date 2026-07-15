import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select

from app.billing.models import Wallet, WalletTransaction
from app.billing.service import InsufficientPointsError, WalletService
from app.db.session import SessionFactory
from app.identity.models import User


@pytest.mark.asyncio
async def test_concurrent_reservations_cannot_overdraw_wallet() -> None:
    user_id = str(uuid4())
    now = datetime.now(UTC).replace(tzinfo=None)

    async with SessionFactory.begin() as setup:
        setup.add(User(
            id=user_id,
            nickname="并发测试用户",
            role="user",
            status="active",
            created_at=now,
            updated_at=now,
        ))
        await setup.flush()
        setup.add(Wallet(
            user_id=user_id,
            balance=10,
            reserved=0,
            version=0,
            updated_at=now,
        ))

    ready_count = 0
    ready_lock = asyncio.Lock()
    start = asyncio.Event()

    async def reserve(reference_id: str):
        nonlocal ready_count
        async with SessionFactory() as session:
            async with session.begin():
                async with ready_lock:
                    ready_count += 1
                    if ready_count == 10:
                        start.set()
                await start.wait()
                return await WalletService(session).reserve(
                    user_id,
                    10,
                    f"concurrency:{user_id}:{reference_id}",
                    reference_id,
                )

    try:
        results = await asyncio.gather(
            *(reserve(f"call-{index}") for index in range(10)),
            return_exceptions=True,
        )

        assert sum(isinstance(result, InsufficientPointsError) for result in results) == 9
        assert sum(not isinstance(result, BaseException) for result in results) == 1

        async with SessionFactory() as verify:
            wallet = await verify.get(Wallet, user_id)
            reserve_count = await verify.scalar(
                select(func.count(WalletTransaction.id)).where(
                    WalletTransaction.user_id == user_id,
                    WalletTransaction.kind == "reserve",
                )
            )

        assert wallet is not None
        assert (wallet.balance, wallet.reserved) == (0, 10)
        assert reserve_count == 1
    finally:
        async with SessionFactory.begin() as cleanup:
            await cleanup.execute(delete(User).where(User.id == user_id))


@pytest.mark.asyncio
async def test_concurrent_duplicate_reservation_is_idempotent() -> None:
    user_id = str(uuid4())
    now = datetime.now(UTC).replace(tzinfo=None)

    async with SessionFactory.begin() as setup:
        setup.add(User(
            id=user_id,
            nickname="幂等并发测试用户",
            role="user",
            status="active",
            created_at=now,
            updated_at=now,
        ))
        await setup.flush()
        setup.add(Wallet(
            user_id=user_id,
            balance=10,
            reserved=0,
            version=0,
            updated_at=now,
        ))

    ready_count = 0
    ready_lock = asyncio.Lock()
    start = asyncio.Event()
    idempotency_key = f"concurrency:{user_id}:same-call"

    async def reserve():
        nonlocal ready_count
        async with SessionFactory() as session:
            async with session.begin():
                async with ready_lock:
                    ready_count += 1
                    if ready_count == 2:
                        start.set()
                await start.wait()
                return await WalletService(session).reserve(
                    user_id,
                    10,
                    idempotency_key,
                    "same-call",
                )

    try:
        results = await asyncio.gather(reserve(), reserve(), return_exceptions=True)

        assert all(not isinstance(result, BaseException) for result in results)

        async with SessionFactory() as verify:
            wallet = await verify.get(Wallet, user_id)
            reserve_count = await verify.scalar(
                select(func.count(WalletTransaction.id)).where(
                    WalletTransaction.user_id == user_id,
                    WalletTransaction.kind == "reserve",
                )
            )

        assert wallet is not None
        assert (wallet.balance, wallet.reserved) == (0, 10)
        assert reserve_count == 1
    finally:
        async with SessionFactory.begin() as cleanup:
            await cleanup.execute(delete(User).where(User.id == user_id))
