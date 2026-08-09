from datetime import UTC, datetime
from dataclasses import dataclass
from collections.abc import Sequence
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.models import (
    TenantUserQuotaPolicy,
    TenantUserQuotaUsage,
    TenantWallet,
    TenantWalletTransaction,
    Wallet,
    WalletTransaction,
)
from app.tenancy.models import TenantMembership


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

    @staticmethod
    def _compat_run_id(reference_id: str) -> str:
        """Provide a bounded synthetic run id for the legacy facade.

        The tenant ledger accepts direct WalletService calls used by old
        compatibility callers, which do not have an AgentRun yet.  Keep the
        audit value deterministic and within the UUID column width; real Pi
        and agent paths always pass their actual run id through
        ``McpPreflightContext``.
        """
        return str(uuid5(NAMESPACE_URL, f"wallet:{reference_id}"))

    async def _tenant_membership(self, user_id: str) -> TenantMembership | None:
        return await self.db.scalar(
            select(TenantMembership).where(TenantMembership.user_id == user_id)
        )

    async def _tenant_wallet(self, user_id: str, *, for_update: bool = False) -> tuple[str, TenantWallet] | None:
        membership = await self._tenant_membership(user_id)
        if membership is not None and membership.status != "active":
            raise LookupError("tenant_membership_disabled")
        statement = (
            select(TenantMembership.tenant_id, TenantWallet)
            .join(TenantWallet, TenantWallet.tenant_id == TenantMembership.tenant_id)
            .where(TenantMembership.user_id == user_id, TenantMembership.status == "active")
        )
        if for_update:
            statement = statement.with_for_update()
        row = (await self.db.execute(statement)).first()
        return None if row is None else (row[0], row[1])

    async def _reject_unprovisioned_tenant_wallet(self, user_id: str) -> None:
        """Prevent normal write paths from falling back to the legacy ledger.

        A legacy row remains readable for historical fixtures, but once a user
        belongs to a tenant, a missing tenant wallet is a provisioning fault,
        not permission to create a second source of truth.
        """
        if await self._tenant_membership(user_id) is not None:
            raise LookupError("tenant_wallet_not_provisioned")

    @staticmethod
    def _month_window(now: datetime) -> tuple[datetime, datetime]:
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
        return start, end

    async def quota_view(self, user_id: str) -> tuple[int, int]:
        """Return ``(available, reserved)`` for the user's tenant quota.

        The available amount is the intersection of the tenant pool's current
        balance and the user's current monthly quota.  It is deliberately a
        read-only projection; the tenant pool remains the single spendable
        ledger.
        """
        tenant = await self._tenant_wallet(user_id)
        if tenant is None:
            wallet = await self.get_wallet(user_id, tenant_source=False)
            return wallet.balance, wallet.reserved
        tenant_id, wallet = tenant
        policy = await self.db.scalar(
            select(TenantUserQuotaPolicy).where(
                TenantUserQuotaPolicy.tenant_id == tenant_id,
                TenantUserQuotaPolicy.user_id == user_id,
                TenantUserQuotaPolicy.period == "monthly",
                TenantUserQuotaPolicy.status == "active",
            )
        )
        if policy is None:
            return 0, 0
        start, end = self._month_window(utc_now())
        usage = await self.db.scalar(
            select(TenantUserQuotaUsage).where(
                TenantUserQuotaUsage.tenant_id == tenant_id,
                TenantUserQuotaUsage.user_id == user_id,
                TenantUserQuotaUsage.period_start == start,
                TenantUserQuotaUsage.period_end == end,
            )
        )
        spent = usage.spent if usage is not None else 0
        reserved = usage.reserved if usage is not None else 0
        remaining = max(0, policy.points_limit - spent - reserved)
        return min(wallet.balance, remaining), reserved

    async def available_points(self, user_id: str) -> int:
        return (await self.quota_view(user_id))[0]

    @staticmethod
    def _wallet_view(user_id: str, wallet: TenantWallet) -> Wallet:
        """Detached compatibility projection; never flushed back to ``wallets``."""
        return Wallet(
            user_id=user_id,
            balance=wallet.balance,
            reserved=wallet.reserved,
            version=wallet.version,
            updated_at=wallet.updated_at,
        )

    async def get_wallet(
        self,
        user_id: str,
        *,
        for_update: bool = False,
        tenant_source: bool = True,
    ) -> Wallet:
        tenant = await self._tenant_wallet(user_id, for_update=for_update) if tenant_source else None
        if tenant is not None:
            return self._wallet_view(user_id, tenant[1])
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
        tenant = await self._tenant_wallet(user_id)
        if tenant is None:
            membership = await self._tenant_membership(user_id)
            if membership is not None:
                from app.pi_gateway.accounting import TenantAccountingService

                accounting = TenantAccountingService(self.db)
                await accounting.ensure_tenant_wallet(membership.tenant_id)
                await accounting.ensure_user_quota(membership.tenant_id, user_id)
                tenant = await self._tenant_wallet(user_id)
        if tenant is not None:
            from app.pi_gateway.accounting import TenantAccountingService

            wallet = await TenantAccountingService(self.db).grant_welcome(tenant[0], user_id)
            return self._wallet_view(user_id, wallet)
        await self._reject_unprovisioned_tenant_wallet(user_id)
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
        *,
        tenant_source: bool = True,
    ) -> Wallet:
        if amount <= 0:
            raise ValueError("amount_must_be_positive")
        # Agent tool calls are part of the unified tenant ledger as well.  The
        # explicit ``tenant_source=False`` escape hatch remains available only
        # to legacy compatibility callers (for example pre-migration
        # fixtures), never by reference type alone.
        use_tenant = tenant_source
        tenant = await self._tenant_wallet(user_id) if use_tenant else None
        if use_tenant and tenant is None:
            await self._reject_unprovisioned_tenant_wallet(user_id)
        if tenant is not None and amount != 10:
            raise ValueError("mcp_cost_fixed")
        if tenant is not None and amount == 10:
            from app.pi_gateway.accounting import McpPreflightContext, TenantAccountingService

            await TenantAccountingService(self.db).reserve_mcp_call(
                McpPreflightContext(
                    tenant_id=tenant[0],
                    user_id=user_id,
                    run_id=self._compat_run_id(reference_id),
                    tool_call_id=reference_id,
                    internal_tool_name=reference_type,
                    service_slug=reference_type,
                    arguments={},
                    feature="billing",
                )
            )
            current = await self._tenant_wallet(user_id)
            if current is None:  # pragma: no cover - row was just locked
                raise LookupError("wallet_not_found")
            return self._wallet_view(user_id, current[1])
        if await self._already_applied(idempotency_key):
            return await self.get_wallet(user_id, tenant_source=use_tenant)
        wallet = await self.get_wallet(user_id, for_update=True, tenant_source=use_tenant)
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
        tenant = await self._tenant_wallet(user_id)
        if tenant is not None:
            from app.pi_gateway.accounting import McpPreflightContext, TenantAccountingService

            accounting = TenantAccountingService(self.db)
            async with self.db.begin_nested():
                for request in requests:
                    await accounting.reserve_mcp_call(
                        McpPreflightContext(
                            tenant_id=tenant[0],
                            user_id=user_id,
                            run_id=self._compat_run_id(request.reference_id),
                            tool_call_id=request.reference_id,
                            internal_tool_name=reference_type,
                            service_slug=reference_type,
                            arguments={},
                            feature="billing",
                        )
                    )
            current = await self._tenant_wallet(user_id)
            if current is None:  # pragma: no cover - row was just locked
                raise LookupError("wallet_not_found")
            return self._wallet_view(user_id, current[1])
        await self._reject_unprovisioned_tenant_wallet(user_id)
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
        *,
        tenant_source: bool = True,
    ) -> Wallet:
        if amount <= 0:
            raise ValueError("amount_must_be_positive")
        use_tenant = tenant_source
        tenant = await self._tenant_wallet(user_id) if use_tenant else None
        if use_tenant and tenant is None:
            await self._reject_unprovisioned_tenant_wallet(user_id)
        if tenant is not None and amount != 10:
            raise ValueError("mcp_cost_fixed")
        if tenant is not None and amount == 10:
            from app.pi_gateway.accounting import TenantAccountingService

            permit_id = await self.db.scalar(
                select(TenantWalletTransaction.id)
                .where(
                    TenantWalletTransaction.tenant_id == tenant[0],
                    TenantWalletTransaction.user_id == user_id,
                    TenantWalletTransaction.tool_call_id == reference_id,
                    TenantWalletTransaction.kind == "reserve",
                )
                .order_by(TenantWalletTransaction.created_at.desc())
                .limit(1)
            )
            if permit_id is None:
                raise ValueError("tenant_mcp_permit_not_found")
            await TenantAccountingService(self.db).settle_mcp_call(permit_id, {"mode": "mcpResult"})
            current = await self._tenant_wallet(user_id)
            if current is None:  # pragma: no cover
                raise LookupError("wallet_not_found")
            return self._wallet_view(user_id, current[1])
        if await self._already_applied(idempotency_key):
            return await self.get_wallet(user_id, tenant_source=use_tenant)
        wallet = await self.get_wallet(user_id, for_update=True, tenant_source=use_tenant)
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
        *,
        tenant_source: bool = True,
    ) -> Wallet:
        if amount <= 0:
            raise ValueError("amount_must_be_positive")
        use_tenant = tenant_source
        tenant = await self._tenant_wallet(user_id) if use_tenant else None
        if use_tenant and tenant is None:
            await self._reject_unprovisioned_tenant_wallet(user_id)
        if tenant is not None and amount != 10:
            raise ValueError("mcp_cost_fixed")
        if tenant is not None and amount == 10:
            from app.pi_gateway.accounting import TenantAccountingService

            permit_id = await self.db.scalar(
                select(TenantWalletTransaction.id)
                .where(
                    TenantWalletTransaction.tenant_id == tenant[0],
                    TenantWalletTransaction.user_id == user_id,
                    TenantWalletTransaction.tool_call_id == reference_id,
                    TenantWalletTransaction.kind == "reserve",
                )
                .order_by(TenantWalletTransaction.created_at.desc())
                .limit(1)
            )
            if permit_id is None:
                raise ValueError("tenant_mcp_permit_not_found")
            await TenantAccountingService(self.db).fail_mcp_call(permit_id, "failed_confirmed")
            current = await self._tenant_wallet(user_id)
            if current is None:  # pragma: no cover
                raise LookupError("wallet_not_found")
            return self._wallet_view(user_id, current[1])
        if await self._already_applied(idempotency_key):
            return await self.get_wallet(user_id, tenant_source=use_tenant)
        wallet = await self.get_wallet(user_id, for_update=True, tenant_source=use_tenant)
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
        tenant = await self._tenant_wallet(user_id)
        if tenant is None:
            membership = await self._tenant_membership(user_id)
            if membership is not None:
                from app.pi_gateway.accounting import TenantAccountingService

                accounting = TenantAccountingService(self.db)
                await accounting.ensure_tenant_wallet(membership.tenant_id)
                await accounting.ensure_user_quota(membership.tenant_id, user_id)
                tenant = await self._tenant_wallet(user_id)
        if tenant is not None:
            from app.pi_gateway.accounting import TenantAccountingService

            wallet, transaction = await TenantAccountingService(self.db).admin_adjust(
                tenant[0], user_id, delta=delta, idempotency_key=idempotency_key, reference_id=reference_id
            )
            # Compatibility callers still expect a WalletTransaction-like
            # object.  The real immutable row lives in tenant_wallet_transactions.
            legacy = WalletTransaction(
                id=transaction.id,
                user_id=user_id,
                kind=transaction.kind,
                balance_delta=transaction.balance_delta,
                reserved_delta=transaction.reserved_delta,
                balance_after=transaction.balance_after,
                reserved_after=transaction.reserved_after,
                idempotency_key=transaction.idempotency_key,
                reference_type=transaction.reference_type,
                reference_id=transaction.reference_id,
                created_at=transaction.created_at,
            )
            return self._wallet_view(user_id, wallet), legacy
        await self._reject_unprovisioned_tenant_wallet(user_id)
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
