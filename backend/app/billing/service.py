from datetime import UTC, datetime
from dataclasses import dataclass
from collections.abc import Sequence
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.models import Wallet, WalletTransaction


class InsufficientPointsError(Exception):
    """Raised when available points cannot cover a reservation."""


@dataclass(frozen=True)
class ReservationRequest:
    reference_id: str
    idempotency_key: str
    amount: int = 10


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class WalletService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_wallet(self, user_id: str, *, for_update: bool = False) -> Wallet:
        statement = select(Wallet).where(Wallet.user_id == user_id)
        if for_update:
            statement = statement.with_for_update()
        wallet = await self.db.scalar(statement)
        if wallet is None:
            raise LookupError("wallet_not_found")
        return wallet

    async def _already_applied(
        self, idempotency_key: str, *, for_update: bool = False
    ) -> bool:
        statement = select(WalletTransaction.id).where(
            WalletTransaction.idempotency_key == idempotency_key
        )
        if for_update:
            statement = statement.with_for_update()
        return await self.db.scalar(statement) is not None

    async def _record(
        self,
        wallet: Wallet,
        *,
        kind: str,
        balance_delta: int,
        reserved_delta: int,
        idempotency_key: str,
        reference_type: str,
        reference_id: str,
    ) -> Wallet:
        wallet.balance += balance_delta
        wallet.reserved += reserved_delta
        wallet.version += 1
        wallet.updated_at = utc_now()
        self.db.add(
            WalletTransaction(
                id=str(uuid4()),
                user_id=wallet.user_id,
                kind=kind,
                balance_delta=balance_delta,
                reserved_delta=reserved_delta,
                balance_after=wallet.balance,
                reserved_after=wallet.reserved,
                idempotency_key=idempotency_key,
                reference_type=reference_type,
                reference_id=reference_id,
                created_at=utc_now(),
            )
        )
        await self.db.flush()
        return wallet

    async def ensure_welcome_grant(self, user_id: str) -> Wallet:
        idempotency_key = f"welcome-grant:{user_id}"
        wallet = await self.db.get(Wallet, user_id)
        if wallet is None:
            wallet = Wallet(
                user_id=user_id,
                balance=0,
                reserved=0,
                version=0,
                updated_at=utc_now(),
            )
            self.db.add(wallet)
            await self.db.flush()
        if await self._already_applied(idempotency_key):
            return wallet
        wallet = await self.get_wallet(user_id, for_update=True)
        if await self._already_applied(idempotency_key, for_update=True):
            return wallet
        return await self._record(
            wallet,
            kind="welcome_grant",
            balance_delta=1000,
            reserved_delta=0,
            idempotency_key=idempotency_key,
            reference_type="user",
            reference_id=user_id,
        )

    async def reserve(
        self,
        user_id: str,
        amount: int,
        idempotency_key: str,
        reference_id: str,
        reference_type: str = "mcp_call",
    ) -> Wallet:
        if amount <= 0:
            raise ValueError("amount_must_be_positive")
        if await self._already_applied(idempotency_key):
            return await self.get_wallet(user_id)
        wallet = await self.get_wallet(user_id, for_update=True)
        if await self._already_applied(idempotency_key, for_update=True):
            return wallet
        if wallet.balance < amount:
            raise InsufficientPointsError()
        return await self._record(
            wallet,
            kind="reserve",
            balance_delta=-amount,
            reserved_delta=amount,
            idempotency_key=idempotency_key,
            reference_type=reference_type,
            reference_id=reference_id,
        )

    async def reserve_batch(
        self,
        user_id: str,
        requests: Sequence[ReservationRequest],
        reference_type: str = "mcp_call",
    ) -> Wallet:
        if not requests or any(request.amount != 10 for request in requests):
            raise ValueError("invalid_mcp_reservation_batch")
        wallet = await self.get_wallet(user_id, for_update=True)
        unapplied = [
            request for request in requests if not await self._already_applied(request.idempotency_key)
        ]
        required = sum(request.amount for request in unapplied)
        if wallet.balance < required:
            raise InsufficientPointsError()
        for request in unapplied:
            await self._record(
                wallet,
                kind="reserve",
                balance_delta=-request.amount,
                reserved_delta=request.amount,
                idempotency_key=request.idempotency_key,
                reference_type=reference_type,
                reference_id=request.reference_id,
            )
        return wallet

    async def settle(
        self,
        user_id: str,
        amount: int,
        idempotency_key: str,
        reference_id: str,
        reference_type: str = "mcp_call",
    ) -> Wallet:
        if amount <= 0:
            raise ValueError("amount_must_be_positive")
        if await self._already_applied(idempotency_key):
            return await self.get_wallet(user_id)
        wallet = await self.get_wallet(user_id, for_update=True)
        if await self._already_applied(idempotency_key, for_update=True):
            return wallet
        if wallet.reserved < amount:
            raise ValueError("invalid_reserved_amount")
        return await self._record(
            wallet,
            kind="settle",
            balance_delta=0,
            reserved_delta=-amount,
            idempotency_key=idempotency_key,
            reference_type=reference_type,
            reference_id=reference_id,
        )

    async def release(
        self,
        user_id: str,
        amount: int,
        idempotency_key: str,
        reference_id: str,
        reference_type: str = "mcp_call",
    ) -> Wallet:
        if amount <= 0:
            raise ValueError("amount_must_be_positive")
        if await self._already_applied(idempotency_key):
            return await self.get_wallet(user_id)
        wallet = await self.get_wallet(user_id, for_update=True)
        if await self._already_applied(idempotency_key, for_update=True):
            return wallet
        if wallet.reserved < amount:
            raise ValueError("invalid_reserved_amount")
        return await self._record(
            wallet,
            kind="release",
            balance_delta=amount,
            reserved_delta=-amount,
            idempotency_key=idempotency_key,
            reference_type=reference_type,
            reference_id=reference_id,
        )

    async def admin_adjust(
        self,
        user_id: str,
        *,
        delta: int,
        reason: str,
        idempotency_key: str,
        reference_id: str,
    ) -> tuple[Wallet, WalletTransaction]:
        """Adjust balance by an administrator. The human-readable reason lives in
        the admin audit log referenced by reference_id, not on the ledger row."""
        if delta == 0:
            raise ValueError("delta_must_be_nonzero")
        statement = select(WalletTransaction).where(
            WalletTransaction.idempotency_key == idempotency_key
        )
        applied = await self.db.scalar(statement)
        if applied is not None:
            return await self.get_wallet(user_id), applied
        wallet = await self.db.get(Wallet, user_id, with_for_update=True)
        if wallet is None:
            wallet = Wallet(
                user_id=user_id,
                balance=0,
                reserved=0,
                version=0,
                updated_at=utc_now(),
            )
            self.db.add(wallet)
            await self.db.flush()
        applied = await self.db.scalar(statement.with_for_update())
        if applied is not None:
            return wallet, applied
        if delta < 0 and wallet.balance + delta < 0:
            raise InsufficientPointsError()
        await self._record(
            wallet,
            kind="admin_adjust",
            balance_delta=delta,
            reserved_delta=0,
            idempotency_key=idempotency_key,
            reference_type="admin_adjust",
            reference_id=reference_id,
        )
        transaction = await self.db.scalar(statement)
        if transaction is None:  # pragma: no cover - _record just flushed it
            raise LookupError("admin_adjust_transaction_missing")
        return wallet, transaction
