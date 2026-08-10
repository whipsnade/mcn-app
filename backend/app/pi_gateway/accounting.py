"""Tenant-scoped MCP preflight and settlement accounting.

This module is deliberately independent of any provider SDK.  FastAPI owns the
permit transaction; a Gateway may call an adapter only after the transaction
containing the reservation has been committed by its caller.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.models import (
    RuntimeUsageRecord,
    TenantUserQuotaPolicy,
    TenantUserQuotaUsage,
    TenantWallet,
    TenantWalletTransaction,
    Wallet,
)
from app.agent_runtime.models import AgentRun, AgentRunAttempt, AgentToolCall
from app.billing.service import InsufficientPointsError
from app.tenancy.models import SUPPORTED_LICENSE_FEATURES, TenantMembership


MCP_POINTS_COST = 10
FailureClassification = Literal["definitely_not_sent", "failed_confirmed", "result_unknown"]


class TenantAccountingError(ValueError):
    """Stable, non-sensitive tenant accounting failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class RuntimeUsageError(ValueError):
    """Stable, non-sensitive usage ingestion or reconciliation failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class UsageAggregate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str
    user_id: str | None = None
    run_id: str | None = None
    day: str | None = None
    record_count: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost_micros: int | None = None
    priced_cost_micros: int = 0
    usage_unavailable_count: int = 0
    unpriced_count: int = 0


class UsageReconciliation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    tenant_id: str
    reconciliation_status: Literal["match", "mismatch"]
    mismatch_codes: tuple[str, ...] = ()
    mcp_settled_points: int = 0
    run_reserved_points: int = 0
    tenant_reserved_points: int = 0
    unknown_reserved_points: int = 0


def _usage_integer(payload: Mapping[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 10**12:
        raise RuntimeUsageError("runtime_usage_value_invalid")
    return value


def _request_id(payload: Mapping[str, Any]) -> str | None:
    value = payload.get("upstream_request_id", payload.get("request_id"))
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 128:
        raise RuntimeUsageError("runtime_usage_request_id_invalid")
    return value


def _source_sequence(attempt_id: str, source_event_id: str) -> int:
    prefix = f"{attempt_id}:"
    if not source_event_id.startswith(prefix):
        raise RuntimeUsageError("runtime_usage_attempt_mismatch")
    suffix = source_event_id[len(prefix) :]
    if not suffix.isdigit() or int(suffix) < 1:
        raise RuntimeUsageError("runtime_usage_source_event_invalid")
    return int(suffix)


def _price_table(snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    billing = snapshot.get("billing")
    if isinstance(billing, Mapping):
        table = billing.get("price_table")
        if isinstance(table, Mapping):
            return table
        return billing
    table = snapshot.get("price_table")
    return table if isinstance(table, Mapping) else {}


def _rate(table: Mapping[str, Any], metric: str) -> int | None:
    per_million_key = f"{metric}_micros_per_million"
    per_token_key = f"{metric}_micros_per_token"
    for key in (per_million_key, per_token_key):
        value = table.get(key)
        if value is not None:
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                or value > (10**12 if key == per_million_key else 10**6)
            ):
                raise RuntimeUsageError("runtime_usage_price_invalid")
            return value if key == per_million_key else value * 1_000_000
    return None


def _ceil_micros(tokens: int, micros_per_million: int) -> int:
    return (tokens * micros_per_million + 999_999) // 1_000_000


def _snapshot_model(run: AgentRun) -> tuple[str, str]:
    snapshot = run.runtime_config_snapshot_json or {}
    model = snapshot.get("model") if isinstance(snapshot, Mapping) else None
    if not isinstance(model, Mapping):
        model = {}
    provider = model.get("provider")
    name = model.get("name")
    return (
        provider if isinstance(provider, str) and provider else "unknown",
        name if isinstance(name, str) and name else run.model,
    )


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
    # DNR 单次真实重试按 dispatch 次数区分账务幂等键（第二次派发真实收费）。
    dispatch_count: int = Field(default=1, ge=1, le=100)

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


class RuntimeUsageService:
    """Persist immutable model usage and run read-only reconciliation results."""

    _TOKEN_FIELDS = (
        ("input_tokens", "input"),
        ("output_tokens", "output"),
        ("cache_read_tokens", "cache_read"),
        ("cache_write_tokens", "cache_write"),
    )

    def __init__(self, db: AsyncSession, *, now_fn=utc_now) -> None:
        self.db = db
        self.now_fn = now_fn

    async def record_model_usage(
        self,
        run: AgentRun,
        attempt_id: str,
        source_event_id: str,
        payload: Mapping[str, Any],
    ) -> RuntimeUsageRecord:
        if run.runtime_backend not in {"pi", "current"} or not run.tenant_id or not run.user_id:
            raise RuntimeUsageError("runtime_usage_run_invalid")
        if not isinstance(payload, Mapping):
            raise RuntimeUsageError("runtime_usage_payload_invalid")
        _source_sequence(attempt_id, source_event_id)
        attempt = await self.db.scalar(
            select(AgentRunAttempt).where(
                AgentRunAttempt.id == attempt_id,
                AgentRunAttempt.run_id == run.id,
            )
        )
        if attempt is None:
            raise RuntimeUsageError("runtime_usage_attempt_mismatch")
        values = {field: _usage_integer(payload, field) for field, _metric in self._TOKEN_FIELDS}
        usage_available = any(value is not None for value in values.values())
        upstream_request_id = _request_id(payload)
        provider, model = _snapshot_model(run)
        table = _price_table(run.runtime_config_snapshot_json or {})
        currency = table.get("currency")
        if currency is not None and (not isinstance(currency, str) or len(currency) > 8):
            raise RuntimeUsageError("runtime_usage_currency_invalid")
        rates = {metric: _rate(table, metric) for _field, metric in self._TOKEN_FIELDS}
        if not usage_available:
            cost_micros = None
            cost_status = "unavailable"
            usage_status = "unavailable"
        elif any(
            values[field] is not None and rates[metric] is None
            for field, metric in self._TOKEN_FIELDS
        ):
            cost_micros = None
            cost_status = "unpriced"
            usage_status = "available"
        else:
            cost_micros = sum(
                _ceil_micros(values[field], rates[metric])
                for field, metric in self._TOKEN_FIELDS
                if values[field] is not None and rates[metric] is not None
            )
            cost_status = "priced"
            usage_status = "available"
        unique_filter = (
            RuntimeUsageRecord.run_id == run.id,
            RuntimeUsageRecord.attempt_id == attempt_id,
            RuntimeUsageRecord.source_event_id == source_event_id,
            RuntimeUsageRecord.kind == "model",
        )
        existing = await self.db.scalar(select(RuntimeUsageRecord).where(*unique_filter))
        if existing is not None:
            if not self._same_usage(existing, values, provider, model, upstream_request_id):
                raise RuntimeUsageError("runtime_usage_idempotency_conflict")
            return existing
        if upstream_request_id is not None:
            request_existing = await self.db.scalar(
                select(RuntimeUsageRecord).where(
                    RuntimeUsageRecord.tenant_id == run.tenant_id,
                    RuntimeUsageRecord.upstream_request_id == upstream_request_id,
                    RuntimeUsageRecord.kind == "model",
                )
            )
            if request_existing is not None:
                raise RuntimeUsageError("runtime_usage_upstream_request_duplicate")
        record = RuntimeUsageRecord(
            id=str(uuid4()),
            tenant_id=run.tenant_id,
            user_id=run.user_id,
            run_id=run.id,
            attempt_id=attempt_id,
            source_event_id=source_event_id,
            kind="model",
            backend=run.runtime_backend,
            provider=provider,
            model=model,
            input_tokens=values["input_tokens"],
            output_tokens=values["output_tokens"],
            cache_read_tokens=values["cache_read_tokens"],
            cache_write_tokens=values["cache_write_tokens"],
            cost_micros=cost_micros,
            currency=currency if isinstance(currency, str) else None,
            upstream_request_id=upstream_request_id,
            usage_status=usage_status,
            cost_status=cost_status,
            metadata_json=None,
            observed_at=self.now_fn(),
        )
        try:
            async with self.db.begin_nested():
                self.db.add(record)
                await self.db.flush()
        except IntegrityError as exc:
            existing = await self.db.scalar(select(RuntimeUsageRecord).where(*unique_filter))
            if existing is not None:
                if self._same_usage(existing, values, provider, model, upstream_request_id):
                    return existing
                raise RuntimeUsageError("runtime_usage_idempotency_conflict") from exc
            if upstream_request_id is not None:
                request_existing = await self.db.scalar(
                    select(RuntimeUsageRecord).where(
                        RuntimeUsageRecord.tenant_id == run.tenant_id,
                        RuntimeUsageRecord.upstream_request_id == upstream_request_id,
                        RuntimeUsageRecord.kind == "model",
                    )
                )
                if request_existing is not None:
                    raise RuntimeUsageError("runtime_usage_upstream_request_duplicate") from exc
            raise RuntimeUsageError("runtime_usage_write_conflict") from exc
        return record

    async def record_usage(
        self,
        run: AgentRun,
        attempt_id: str,
        source_event_id: str,
        payload: Mapping[str, Any],
    ) -> RuntimeUsageRecord:
        return await self.record_model_usage(run, attempt_id, source_event_id, payload)

    @staticmethod
    def _same_usage(
        record: RuntimeUsageRecord,
        values: Mapping[str, int | None],
        provider: str,
        model: str,
        upstream_request_id: str | None,
    ) -> bool:
        return (
            record.provider == provider
            and record.model == model
            and record.upstream_request_id == upstream_request_id
            and all(getattr(record, field) == values[field] for field in values)
        )

    async def reconcile_run(self, run_id: str) -> UsageReconciliation:
        run = await self.db.scalar(select(AgentRun).where(AgentRun.id == run_id))
        if run is None or not run.tenant_id:
            raise RuntimeUsageError("runtime_usage_run_not_found")
        wallet = await self.db.scalar(
            select(TenantWallet).where(TenantWallet.tenant_id == run.tenant_id)
        )
        if wallet is None:
            raise RuntimeUsageError("runtime_usage_wallet_not_found")
        transactions = list(
            (
                await self.db.scalars(
                    select(TenantWalletTransaction).where(
                        TenantWalletTransaction.tenant_id == run.tenant_id
                    )
                )
            ).all()
        )
        tenant_reserved_points, _tenant_settled, _tenant_unknown = self._ledger_totals(transactions)
        run_transactions = [row for row in transactions if row.run_id == run.id]
        run_reserved_points, mcp_settled_points, unknown_reserved_points = self._ledger_totals(
            run_transactions
        )
        mismatch_codes: list[str] = []
        if wallet.reserved != tenant_reserved_points:
            mismatch_codes.append("tenant_reserved_mismatch")
        if run_reserved_points < unknown_reserved_points:
            mismatch_codes.append("unknown_reserved_mismatch")
        calls = list(
            (
                await self.db.scalars(
                    select(AgentToolCall).where(AgentToolCall.run_id == run.id)
                )
            ).all()
        )
        # An empty ToolCall set is still a fact: any ledger settlement or
        # unknown reservation for this Run is then an orphan and must be
        # surfaced.  Do not skip this comparison merely because the caller
        # row was deleted or never materialized.
        settled_from_calls = sum(call.points_settled for call in calls)
        unknown_from_calls = sum(10 for call in calls if call.status == "unknown")
        if settled_from_calls != mcp_settled_points:
            mismatch_codes.append("tool_call_settled_mismatch")
        if unknown_from_calls != unknown_reserved_points:
            mismatch_codes.append("tool_call_unknown_mismatch")
        return UsageReconciliation(
            run_id=run.id,
            tenant_id=run.tenant_id,
            reconciliation_status="mismatch" if mismatch_codes else "match",
            mismatch_codes=tuple(mismatch_codes),
            mcp_settled_points=mcp_settled_points,
            run_reserved_points=run_reserved_points,
            tenant_reserved_points=tenant_reserved_points,
            unknown_reserved_points=unknown_reserved_points,
        )

    @staticmethod
    def _ledger_totals(
        rows: list[TenantWalletTransaction],
    ) -> tuple[int, int, int]:
        """Derive outstanding/settled/unknown from each reserve's terminal row.

        A settled row has ``reserved_delta=-10`` while an unknown row keeps the
        reservation at zero delta.  Computing the state per permit avoids
        mistaking a settled call plus a separate unknown call for a zero net
        reservation.
        """

        reserves = {row.tool_call_id or row.id: row for row in rows if row.kind == "reserve"}
        terminals: dict[str, TenantWalletTransaction] = {}
        for row in rows:
            if row.kind in {"settle", "release", "unknown"}:
                key = row.tool_call_id or row.reference_id
                if key is not None:
                    previous = terminals.get(key)
                    if previous is None or row.created_at >= previous.created_at:
                        terminals[key] = row
        outstanding = settled = unknown = 0
        for key in reserves:
            terminal = terminals.get(key)
            if terminal is None or terminal.kind == "unknown":
                outstanding += MCP_POINTS_COST
            if terminal is not None and terminal.kind == "settle":
                settled += MCP_POINTS_COST
            if terminal is not None and terminal.kind == "unknown":
                unknown += MCP_POINTS_COST
        return outstanding, settled, unknown

    async def aggregate_usage(
        self,
        tenant_id: str,
        *,
        user_id: str | None = None,
        run_id: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        group_by: Literal["tenant", "user", "run", "day"] = "day",
    ) -> list[UsageAggregate]:
        if group_by not in {"tenant", "user", "run", "day"}:
            raise RuntimeUsageError("runtime_usage_group_invalid")
        conditions = [RuntimeUsageRecord.tenant_id == tenant_id]
        if user_id is not None:
            conditions.append(RuntimeUsageRecord.user_id == user_id)
        if run_id is not None:
            conditions.append(RuntimeUsageRecord.run_id == run_id)
        if start is not None:
            conditions.append(RuntimeUsageRecord.observed_at >= start)
        if end is not None:
            conditions.append(RuntimeUsageRecord.observed_at < end)
        records = list(
            (
                await self.db.scalars(
                    select(RuntimeUsageRecord)
                    .where(*conditions)
                    .order_by(RuntimeUsageRecord.observed_at, RuntimeUsageRecord.id)
                )
            ).all()
        )
        groups: dict[tuple[object, ...], list[RuntimeUsageRecord]] = {}
        for record in records:
            if group_by == "tenant":
                key = (record.tenant_id,)
            elif group_by == "user":
                key = (record.tenant_id, record.user_id)
            elif group_by == "run":
                key = (record.tenant_id, record.user_id, record.run_id)
            else:
                key = (record.tenant_id, record.observed_at.date().isoformat())
            groups.setdefault(key, []).append(record)
        result: list[UsageAggregate] = []
        for key, rows in groups.items():
            token_totals = {
                field: self._sum_optional(rows, field)
                for field, _metric in self._TOKEN_FIELDS
            }
            priced_cost = sum(
                row.cost_micros for row in rows if row.cost_micros is not None
            )
            all_priced = bool(rows) and all(row.cost_status == "priced" for row in rows)
            user_value = key[1] if group_by in {"user", "run"} else None
            run_value = key[2] if group_by == "run" else None
            day_value = key[1] if group_by == "day" else None
            result.append(
                UsageAggregate(
                    tenant_id=str(key[0]),
                    user_id=str(user_value) if user_value is not None else None,
                    run_id=str(run_value) if run_value is not None else None,
                    day=str(day_value) if day_value is not None else None,
                    record_count=len(rows),
                    **token_totals,
                    cost_micros=priced_cost if all_priced else None,
                    priced_cost_micros=priced_cost,
                    usage_unavailable_count=sum(
                        row.usage_status == "unavailable" for row in rows
                    ),
                    unpriced_count=sum(row.cost_status == "unpriced" for row in rows),
                )
            )
        return result

    @staticmethod
    def _sum_optional(rows: list[RuntimeUsageRecord], field: str) -> int | None:
        values = [getattr(row, field) for row in rows]
        if not any(value is not None for value in values):
            return None
        return sum(value for value in values if value is not None)


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

    @staticmethod
    def reserve_idempotency_key(tool_call_id: str, dispatch_count: int) -> str:
        if dispatch_count <= 1:
            return f"tenant-mcp:{tool_call_id}:reserve"
        return f"tenant-mcp:{tool_call_id}:dispatch:{dispatch_count}:reserve"

    @classmethod
    def _reserve_idempotency_key(cls, context: McpPreflightContext) -> str:
        return cls.reserve_idempotency_key(context.tool_call_id, context.dispatch_count)

    async def reserve_mcp_call(self, context: McpPreflightContext) -> McpPermit:
        await self._membership(context)
        # 每次外发前复核 License 状态/有效期/feature（新旧 Runtime 共用本入口，
        # Run 中途暂停、过期或 feature 关闭都会在下一次调用前阻断）。``billing``
        # 等兼容账务入口不携带营销 feature，不在本门禁范围内。
        if context.feature in SUPPORTED_LICENSE_FEATURES:
            from app.licensing.service import LicenseService

            decision = await LicenseService(self.db).authorize_feature_decision(
                context.tenant_id, context.user_id, context.feature
            )
            if not decision.allowed:
                raise TenantAccountingError(decision.code)
        existing = await self.db.scalar(
            select(TenantWalletTransaction)
            .where(
                TenantWalletTransaction.idempotency_key
                == self._reserve_idempotency_key(context)
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
        wallet = await self.db.scalar(
            select(TenantWallet)
            .where(TenantWallet.tenant_id == context.tenant_id)
            .with_for_update()
        )
        if wallet is None:
            # 置备/迁移故障：fail-closed，不自动开空钱包、不写旧账。
            raise TenantAccountingError("tenant_wallet_missing")
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
                idempotency_key=self._reserve_idempotency_key(context),
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
        idem = f"tenant-mcp:{reserve.id}:settle"
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
                TenantWalletTransaction.reference_id == reserve.id,
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
        idem = f"tenant-mcp:{reserve.id}:release"
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
    "RuntimeUsageError",
    "RuntimeUsageService",
    "TenantAccountingError",
    "TenantAccountingService",
    "TenantWalletInsufficientError",
    "UsageAggregate",
    "UsageReconciliation",
]
