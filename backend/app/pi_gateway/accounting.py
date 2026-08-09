"""Tenant-scoped MCP preflight and settlement accounting.

This module is deliberately independent of any provider SDK.  FastAPI owns the
permit transaction; a Gateway may call an adapter only after the transaction
containing the reservation has been committed by its caller.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, Mapping
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.models import (
    TenantUserQuotaPolicy,
    TenantUserQuotaUsage,
    TenantWallet,
    TenantWalletTransaction,
    Wallet,
)
from app.billing.service import InsufficientPointsError
from app.tenancy.models import TenantMembership


MCP_POINTS_COST = 10
FailureClassification = Literal["definitely_not_sent", "failed_confirmed", "result_unknown"]


class TenantAccountingError(ValueError):
    """Stable, non-sensitive tenant accounting failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class TenantWalletInsufficientError(InsufficientPointsError, TenantAccountingError):
    def __init__(self) -> None:
        TenantAccountingError.__init__(self, "tenant_wallet_insufficient")


class QuotaExceededError(TenantAccountingError):
    def __init__(self) -> None:
        super().__init__("tenant_user_quota_exceeded")


class McpPreflightContext(BaseModel):
    """Server-derived MCP identity and normalized arguments.

    The model intentionally forbids ``amount`` and ``remote_name``.  The
    catalog mapping and fixed price are resolved by the control plane rather
    than supplied by a model or Gateway request body.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    tenant_id: str = Field(min_length=1, max_length=64)
    user_id: str = Field(min_length=1, max_length=64)
    run_id: str = Field(min_length=1, max_length=64)
    tool_call_id: str = Field(min_length=1, max_length=64)
    internal_tool_name: str = Field(min_length=1, max_length=128)
    service_slug: str = Field(min_length=1, max_length=64)
    arguments: dict[str, Any] = Field(default_factory=dict)
    feature: str = Field(min_length=1, max_length=64)

    @field_validator("arguments")
    @classmethod
    def bound_arguments(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > 64:
            raise ValueError("mcp_arguments_too_many")
        return value


class McpPermit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    permit_id: str = Field(min_length=1, max_length=64)
    tenant_id: str = Field(min_length=1, max_length=64)
    user_id: str = Field(min_length=1, max_length=64)
    run_id: str = Field(min_length=1, max_length=64)
    tool_call_id: str = Field(min_length=1, max_length=64)
    catalog_entry_id: str = Field(min_length=1, max_length=64)
    amount: Literal[10] = Field(default=MCP_POINTS_COST, frozen=True)


class EvidenceReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    permit_id: str
    status: Literal["settled"] = "settled"
    payload: dict[str, Any] = Field(default_factory=dict)


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _month_window(now: datetime) -> tuple[datetime, datetime]:
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # Store an exclusive next-month boundary at second precision.  MySQL's
    # default DATETIME precision is second-based; using 23:59:59.999999 would
    # round on insert and make the same usage row impossible to find again.
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


class TenantAccountingService:
    """Lock wallet and user quota together for every MCP settlement."""

    def __init__(self, db: AsyncSession, *, now_fn=utc_now) -> None:
        self.db = db
        self.now_fn = now_fn

    @staticmethod
    def validate_failure_classification(value: str) -> FailureClassification:
        if value not in {"definitely_not_sent", "failed_confirmed", "result_unknown"}:
            raise ValueError("mcp_failure_classification_invalid")
        return value  # type: ignore[return-value]

    async def ensure_tenant_wallet(
        self,
        tenant_id: str,
        *,
        balance: int = 0,
        reserved: int = 0,
    ) -> TenantWallet:
        if balance < 0 or reserved < 0:
            raise ValueError("tenant_wallet_amount_invalid")
        statement = select(TenantWallet).where(TenantWallet.tenant_id == tenant_id)
        wallet = await self.db.scalar(statement.with_for_update())
        if wallet is None:
            legacy = await self.db.scalar(
                select(Wallet)
                .join(TenantMembership, TenantMembership.user_id == Wallet.user_id)
                .where(TenantMembership.tenant_id == tenant_id)
            )
            if legacy is not None and balance == 0 and reserved == 0:
                balance, reserved = legacy.balance, legacy.reserved
            try:
                async with self.db.begin_nested():
                    self.db.add(
                        TenantWallet(
                            tenant_id=tenant_id,
                            balance=balance,
                            reserved=reserved,
                            version=0,
                            updated_at=self.now_fn(),
                        )
                    )
                    await self.db.flush()
            except IntegrityError:
                # Another transaction won first creation.  The outer
                # transaction remains usable and the locked re-read below
                # supplies the authoritative balance.
                pass
            wallet = await self.db.scalar(statement.with_for_update())
        if wallet is None:
            raise TenantAccountingError("tenant_wallet_not_found")
        return wallet

    async def ensure_user_quota(
        self,
        tenant_id: str,
        user_id: str,
        *,
        points_limit: int = 1000,
        status: str = "active",
    ) -> TenantUserQuotaPolicy:
        if points_limit < 0 or status not in {"active", "disabled"}:
            raise ValueError("tenant_user_quota_policy_invalid")
        policy = await self.db.scalar(
            select(TenantUserQuotaPolicy).where(
                TenantUserQuotaPolicy.tenant_id == tenant_id,
                TenantUserQuotaPolicy.user_id == user_id,
                TenantUserQuotaPolicy.period == "monthly",
            )
        )
        if policy is None:
            now = self.now_fn()
            try:
                async with self.db.begin_nested():
                    self.db.add(
                        TenantUserQuotaPolicy(
                            id=str(uuid4()),
                            tenant_id=tenant_id,
                            user_id=user_id,
                            period="monthly",
                            points_limit=points_limit,
                            status=status,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    await self.db.flush()
            except IntegrityError:
                pass
            policy = await self.db.scalar(
                select(TenantUserQuotaPolicy)
                .where(
                    TenantUserQuotaPolicy.tenant_id == tenant_id,
                    TenantUserQuotaPolicy.user_id == user_id,
                    TenantUserQuotaPolicy.period == "monthly",
                )
                .with_for_update()
            )
        if policy is None:
            raise TenantAccountingError("tenant_user_quota_not_found")
        return policy

    async def _membership(self, context: McpPreflightContext) -> TenantMembership:
        membership = await self.db.scalar(
            select(TenantMembership)
            .where(
                TenantMembership.tenant_id == context.tenant_id,
                TenantMembership.user_id == context.user_id,
                TenantMembership.status == "active",
            )
            .with_for_update()
        )
        if membership is None:
            raise TenantAccountingError("tenant_membership_invalid")
        return membership

    async def _usage_rows(
        self, context: McpPreflightContext, *, for_update: bool = True
    ) -> tuple[TenantUserQuotaPolicy, TenantUserQuotaUsage]:
        policy = await self.db.scalar(
            select(TenantUserQuotaPolicy)
            .where(
                TenantUserQuotaPolicy.tenant_id == context.tenant_id,
                TenantUserQuotaPolicy.user_id == context.user_id,
                TenantUserQuotaPolicy.period == "monthly",
            )
            .with_for_update() if for_update else select(TenantUserQuotaPolicy).where(
                TenantUserQuotaPolicy.tenant_id == context.tenant_id,
                TenantUserQuotaPolicy.user_id == context.user_id,
                TenantUserQuotaPolicy.period == "monthly",
            )
        )
        if policy is None:
            policy = await self.ensure_user_quota(context.tenant_id, context.user_id)
        if policy.status != "active":
            raise QuotaExceededError()
        period_start, period_end = _month_window(self.now_fn())
        statement = select(TenantUserQuotaUsage).where(
            TenantUserQuotaUsage.tenant_id == context.tenant_id,
            TenantUserQuotaUsage.user_id == context.user_id,
            TenantUserQuotaUsage.period_start == period_start,
            TenantUserQuotaUsage.period_end == period_end,
        )
        if for_update:
            statement = statement.with_for_update()
        usage = await self.db.scalar(statement)
        if usage is None:
            try:
                async with self.db.begin_nested():
                    self.db.add(
                        TenantUserQuotaUsage(
                            id=str(uuid4()),
                            tenant_id=context.tenant_id,
                            user_id=context.user_id,
                            period_start=period_start,
                            period_end=period_end,
                            spent=0,
                            reserved=0,
                            version=0,
                            updated_at=self.now_fn(),
                        )
                    )
                    await self.db.flush()
            except IntegrityError:
                pass
            usage = await self.db.scalar(statement.with_for_update() if for_update else statement)
        if usage is None:
            raise TenantAccountingError("tenant_user_quota_usage_not_found")
        return policy, usage

    async def _wallet(self, tenant_id: str) -> TenantWallet:
        statement = select(TenantWallet).where(TenantWallet.tenant_id == tenant_id).with_for_update()
        wallet = await self.db.scalar(statement)
        if wallet is None:
            wallet = await self.ensure_tenant_wallet(tenant_id)
            wallet = await self.db.scalar(statement)
        if wallet is None:
            raise TenantAccountingError("tenant_wallet_not_found")
        return wallet

    async def reserve_mcp_call(self, context: McpPreflightContext) -> McpPermit:
        await self._membership(context)
        existing = await self.db.scalar(
            select(TenantWalletTransaction)
            .where(
                TenantWalletTransaction.idempotency_key
                == f"tenant-mcp:{context.tool_call_id}:reserve"
            )
            .with_for_update()
        )
        if existing is not None:
            if (
                existing.tenant_id != context.tenant_id
                or existing.user_id != context.user_id
                or existing.run_id != context.run_id
                or existing.tool_call_id != context.tool_call_id
                or existing.internal_tool_name != context.internal_tool_name
            ):
                raise TenantAccountingError("tenant_accounting_idempotency_conflict")
            return McpPermit(
                permit_id=existing.id,
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                run_id=context.run_id,
                tool_call_id=context.tool_call_id,
                catalog_entry_id=context.internal_tool_name,
            )
        wallet = await self._wallet(context.tenant_id)
        policy, usage = await self._usage_rows(context)
        if wallet.balance < MCP_POINTS_COST:
            raise TenantWalletInsufficientError()
        if usage.spent + usage.reserved + MCP_POINTS_COST > policy.points_limit:
            raise QuotaExceededError()
        now = self.now_fn()
        permit_id = str(uuid4())
        wallet.balance -= MCP_POINTS_COST
        wallet.reserved += MCP_POINTS_COST
        wallet.version += 1
        wallet.updated_at = now
        usage.reserved += MCP_POINTS_COST
        usage.version += 1
        usage.updated_at = now
        self.db.add(
            TenantWalletTransaction(
                id=permit_id,
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                run_id=context.run_id,
                tool_call_id=context.tool_call_id,
                internal_tool_name=context.internal_tool_name,
                kind="reserve",
                balance_delta=-MCP_POINTS_COST,
                reserved_delta=MCP_POINTS_COST,
                balance_after=wallet.balance,
                reserved_after=wallet.reserved,
                idempotency_key=f"tenant-mcp:{context.tool_call_id}:reserve",
                reference_type="mcp_call",
                reference_id=context.tool_call_id,
                created_at=now,
            )
        )
        await self.db.flush()
        return McpPermit(
            permit_id=permit_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            run_id=context.run_id,
            tool_call_id=context.tool_call_id,
            catalog_entry_id=context.internal_tool_name,
        )

    async def grant_welcome(self, tenant_id: str, user_id: str, amount: int = 1000) -> TenantWallet:
        if amount <= 0:
            raise ValueError("welcome_grant_amount_invalid")
        context = McpPreflightContext(
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=f"welcome:{user_id}",
            tool_call_id=f"welcome:{user_id}",
            internal_tool_name="welcome_grant",
            service_slug="billing",
            arguments={},
            feature="billing",
        )
        await self._membership(context)
        wallet = await self._wallet(tenant_id)
        idem = f"welcome-grant:{user_id}"
        if await self.db.scalar(
            select(TenantWalletTransaction.id).where(
                TenantWalletTransaction.idempotency_key == idem
            )
        ):
            return wallet
        now = self.now_fn()
        wallet.balance += amount
        wallet.version += 1
        wallet.updated_at = now
        self.db.add(
            TenantWalletTransaction(
                id=str(uuid4()),
                tenant_id=tenant_id,
                user_id=user_id,
                internal_tool_name="welcome_grant",
                kind="welcome_grant",
                balance_delta=amount,
                reserved_delta=0,
                balance_after=wallet.balance,
                reserved_after=wallet.reserved,
                idempotency_key=idem,
                reference_type="user",
                reference_id=user_id,
                created_at=now,
            )
        )
        await self.db.flush()
        await self.ensure_user_quota(tenant_id, user_id)
        return wallet

    async def admin_adjust(
        self,
        tenant_id: str,
        user_id: str,
        *,
        delta: int,
        idempotency_key: str,
        reference_id: str,
    ) -> tuple[TenantWallet, TenantWalletTransaction]:
        if delta == 0:
            raise ValueError("delta_must_be_nonzero")
        context = McpPreflightContext(
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=f"admin:{reference_id}",
            tool_call_id=f"admin:{reference_id}",
            internal_tool_name="admin_adjust",
            service_slug="billing",
            arguments={},
            feature="billing",
        )
        await self._membership(context)
        existing = await self.db.scalar(
            select(TenantWalletTransaction)
            .where(TenantWalletTransaction.idempotency_key == idempotency_key)
            .with_for_update()
        )
        wallet = await self._wallet(tenant_id)
        if existing is not None:
            if existing.tenant_id != tenant_id or existing.user_id != user_id:
                raise TenantAccountingError("tenant_accounting_idempotency_conflict")
            return wallet, existing
        if wallet.balance + delta < 0:
            raise TenantWalletInsufficientError()
        now = self.now_fn()
        wallet.balance += delta
        wallet.version += 1
        wallet.updated_at = now
        transaction = TenantWalletTransaction(
            id=str(uuid4()),
            tenant_id=tenant_id,
            user_id=user_id,
            internal_tool_name="admin_adjust",
            kind="admin_adjust",
            balance_delta=delta,
            reserved_delta=0,
            balance_after=wallet.balance,
            reserved_after=wallet.reserved,
            idempotency_key=idempotency_key,
            reference_type="admin_adjust",
            reference_id=reference_id,
            created_at=now,
        )
        self.db.add(transaction)
        await self.db.flush()
        return wallet, transaction

    async def settle_mcp_call(self, permit_id: str, payload: object) -> EvidenceReceipt:
        reserve = await self.db.scalar(
            select(TenantWalletTransaction)
            .where(TenantWalletTransaction.id == permit_id)
            .with_for_update()
        )
        if reserve is None or reserve.kind != "reserve":
            raise TenantAccountingError("mcp_permit_not_found")
        idem = f"tenant-mcp:{reserve.tool_call_id}:settle"
        if await self.db.scalar(
            select(TenantWalletTransaction.id).where(
                TenantWalletTransaction.idempotency_key == idem
            )
        ):
            return EvidenceReceipt(permit_id=permit_id, payload=_safe_payload(payload))
        context = McpPreflightContext(
            tenant_id=reserve.tenant_id,
            user_id=reserve.user_id or "",
            run_id=reserve.run_id or "",
            tool_call_id=reserve.tool_call_id or "",
            internal_tool_name="settlement",
            service_slug="settlement",
            arguments={},
            feature="settlement",
        )
        wallet = await self._wallet(reserve.tenant_id)
        _policy, usage = await self._usage_rows(context)
        if wallet.reserved < MCP_POINTS_COST or usage.reserved < MCP_POINTS_COST:
            raise TenantAccountingError("tenant_reserved_amount_invalid")
        now = self.now_fn()
        wallet.reserved -= MCP_POINTS_COST
        wallet.version += 1
        wallet.updated_at = now
        usage.reserved -= MCP_POINTS_COST
        usage.spent += MCP_POINTS_COST
        usage.version += 1
        usage.updated_at = now
        self.db.add(
            TenantWalletTransaction(
                id=str(uuid4()),
                tenant_id=reserve.tenant_id,
                user_id=reserve.user_id,
                run_id=reserve.run_id,
                tool_call_id=reserve.tool_call_id,
                internal_tool_name=reserve.internal_tool_name,
                kind="settle",
                balance_delta=0,
                reserved_delta=-MCP_POINTS_COST,
                balance_after=wallet.balance,
                reserved_after=wallet.reserved,
                idempotency_key=idem,
                reference_type="mcp_call",
                reference_id=permit_id,
                created_at=now,
            )
        )
        await self.db.flush()
        return EvidenceReceipt(permit_id=permit_id, payload=_safe_payload(payload))

    async def fail_mcp_call(self, permit_id: str, classification: str) -> None:
        classification = self.validate_failure_classification(classification)
        reserve = await self.db.scalar(
            select(TenantWalletTransaction)
            .where(TenantWalletTransaction.id == permit_id)
            .with_for_update()
        )
        if reserve is None or reserve.kind != "reserve":
            raise TenantAccountingError("mcp_permit_not_found")
        terminal = await self.db.scalar(
            select(TenantWalletTransaction.id)
            .where(
                TenantWalletTransaction.tool_call_id == reserve.tool_call_id,
                TenantWalletTransaction.kind.in_(("settle", "release")),
            )
            .limit(1)
        )
        if terminal is not None:
            return
        if classification == "result_unknown":
            idem = f"tenant-mcp:{reserve.tool_call_id}:unknown"
            if not await self.db.scalar(
                select(TenantWalletTransaction.id).where(
                    TenantWalletTransaction.idempotency_key == idem
                )
            ):
                context = McpPreflightContext(
                    tenant_id=reserve.tenant_id,
                    user_id=reserve.user_id or "",
                    run_id=reserve.run_id or "",
                    tool_call_id=reserve.tool_call_id or "",
                    internal_tool_name="unknown",
                    service_slug="unknown",
                    arguments={},
                    feature="unknown",
                )
                wallet = await self._wallet(reserve.tenant_id)
                await self._usage_rows(context)
                self.db.add(
                    TenantWalletTransaction(
                        id=str(uuid4()),
                        tenant_id=reserve.tenant_id,
                        user_id=reserve.user_id,
                        run_id=reserve.run_id,
                        tool_call_id=reserve.tool_call_id,
                        internal_tool_name=reserve.internal_tool_name,
                        kind="unknown",
                        balance_delta=0,
                        reserved_delta=0,
                        balance_after=wallet.balance,
                        reserved_after=wallet.reserved,
                        idempotency_key=idem,
                        reference_type="mcp_call",
                        reference_id=permit_id,
                        created_at=self.now_fn(),
                    )
                )
                await self.db.flush()
            return
        idem = f"tenant-mcp:{reserve.tool_call_id}:release"
        if await self.db.scalar(
            select(TenantWalletTransaction.id).where(
                TenantWalletTransaction.idempotency_key == idem
            )
        ):
            return
        context = McpPreflightContext(
            tenant_id=reserve.tenant_id,
            user_id=reserve.user_id or "",
            run_id=reserve.run_id or "",
            tool_call_id=reserve.tool_call_id or "",
            internal_tool_name="release",
            service_slug="release",
            arguments={},
            feature="release",
        )
        wallet = await self._wallet(reserve.tenant_id)
        _policy, usage = await self._usage_rows(context)
        if wallet.reserved < MCP_POINTS_COST or usage.reserved < MCP_POINTS_COST:
            raise TenantAccountingError("tenant_reserved_amount_invalid")
        now = self.now_fn()
        wallet.balance += MCP_POINTS_COST
        wallet.reserved -= MCP_POINTS_COST
        wallet.version += 1
        wallet.updated_at = now
        usage.reserved -= MCP_POINTS_COST
        usage.version += 1
        usage.updated_at = now
        self.db.add(
            TenantWalletTransaction(
                id=str(uuid4()),
                tenant_id=reserve.tenant_id,
                user_id=reserve.user_id,
                run_id=reserve.run_id,
                tool_call_id=reserve.tool_call_id,
                internal_tool_name=reserve.internal_tool_name,
                kind="release",
                balance_delta=MCP_POINTS_COST,
                reserved_delta=-MCP_POINTS_COST,
                balance_after=wallet.balance,
                reserved_after=wallet.reserved,
                idempotency_key=idem,
                reference_type="mcp_call",
                reference_id=permit_id,
                created_at=now,
            )
        )
        await self.db.flush()


def _safe_payload(payload: object) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {"value": str(payload)[:2000]}

    sensitive = {"token", "authorization", "api_key", "apikey", "secret", "password"}

    def sanitize(value: object, depth: int = 0) -> object:
        if depth >= 6:
            return "<truncated>"
        if isinstance(value, Mapping):
            return {
                str(key): sanitize(item, depth + 1)
                for key, item in list(value.items())[:64]
                if str(key).lower() not in sensitive
            }
        if isinstance(value, (list, tuple)):
            return [sanitize(item, depth + 1) for item in list(value)[:64]]
        if isinstance(value, str):
            return value[:2000]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return "<unsupported>"

    return sanitize(payload)  # type: ignore[return-value]


__all__ = [
    "EvidenceReceipt",
    "FailureClassification",
    "McpPermit",
    "McpPreflightContext",
    "MCP_POINTS_COST",
    "QuotaExceededError",
    "TenantAccountingError",
    "TenantAccountingService",
    "TenantWalletInsufficientError",
]
