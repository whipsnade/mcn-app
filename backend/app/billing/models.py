from datetime import datetime

from typing import Any

from sqlalchemy import (
    BigInteger,
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Wallet(Base):
    __tablename__ = "wallets"
    __table_args__ = (
        CheckConstraint("balance >= 0", name="ck_wallet_balance_nonnegative"),
        CheckConstraint("reserved >= 0", name="ck_wallet_reserved_nonnegative"),
    )

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    balance: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class WalletTransaction(Base):
    __tablename__ = "wallet_transactions"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_wallet_tx_idempotency"),
        Index("ix_wallet_transactions_user_id_created_at", "user_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    balance_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_after: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    reference_type: Mapped[str | None] = mapped_column(String(32))
    reference_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class TenantWallet(Base):
    """The single spendable points pool for a tenant.

    ``Wallet`` remains as a read-only compatibility projection for one release
    cycle.  New reservations and settlements are recorded only here.
    """

    __tablename__ = "tenant_wallets"
    __table_args__ = (
        CheckConstraint("balance >= 0", name="ck_tenant_wallet_balance_nonnegative"),
        CheckConstraint("reserved >= 0", name="ck_tenant_wallet_reserved_nonnegative"),
    )

    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    balance: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class TenantWalletTransaction(Base):
    """Append-only tenant ledger entry with globally unique idempotency."""

    __tablename__ = "tenant_wallet_transactions"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_tenant_wallet_tx_idempotency"),
        Index("ix_tenant_wallet_tx_tenant_created", "tenant_id", "created_at"),
        Index("ix_tenant_wallet_tx_reference", "reference_type", "reference_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # The accounting port can be used before a durable ToolCall row exists
    # (the Pi preflight endpoint creates it in the same transaction).  Run and
    # ToolCall ownership is therefore validated by the preflight service, while
    # the immutable ledger keeps their identifiers for audit joins.
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    internal_tool_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    balance_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_after: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    reference_type: Mapped[str | None] = mapped_column(String(48), nullable=True)
    reference_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class TenantUserQuotaPolicy(Base):
    """Monthly points policy for a tenant member."""

    __tablename__ = "tenant_user_quota_policies"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", "period", name="uq_tenant_user_quota_policy"),
        CheckConstraint("period = 'monthly'", name="ck_tenant_user_quota_period"),
        CheckConstraint("points_limit >= 0", name="ck_tenant_user_quota_limit_nonnegative"),
        CheckConstraint("status IN ('active','disabled')", name="ck_tenant_user_quota_status"),
        Index("ix_tenant_user_quota_policy_user_status", "user_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    period: Mapped[str] = mapped_column(String(16), nullable=False, default="monthly")
    points_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class TenantUserQuotaUsage(Base):
    """Mutable monthly counter paired with a tenant wallet reservation."""

    __tablename__ = "tenant_user_quota_usage"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "user_id",
            "period_start",
            "period_end",
            name="uq_tenant_user_quota_usage_period",
        ),
        CheckConstraint("spent >= 0", name="ck_tenant_user_quota_spent_nonnegative"),
        CheckConstraint("reserved >= 0", name="ck_tenant_user_quota_reserved_nonnegative"),
        Index("ix_tenant_user_quota_usage_user_period", "user_id", "period_start", "period_end"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    period_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    spent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class RuntimeUsageRecord(Base):
    """Append-only model/provider usage observation.

    Usage rows are not business-point ledger entries.  The database constraints
    make invalid negative counters and cross-attempt references fail closed;
    pricing and reconciliation remain server-side service decisions.
    """

    __tablename__ = "runtime_usage_records"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "attempt_id", "source_event_id", "kind", name="uq_runtime_usage_event"
        ),
        UniqueConstraint(
            "tenant_id", "kind", "upstream_request_id", name="uq_runtime_usage_upstream_request"
        ),
        CheckConstraint("kind IN ('model','mcp')", name="ck_runtime_usage_kind"),
        CheckConstraint("backend IN ('current','pi')", name="ck_runtime_usage_backend"),
        CheckConstraint(
            "(input_tokens IS NULL OR input_tokens >= 0) AND "
            "(output_tokens IS NULL OR output_tokens >= 0) AND "
            "(cache_read_tokens IS NULL OR cache_read_tokens >= 0) AND "
            "(cache_write_tokens IS NULL OR cache_write_tokens >= 0)",
            name="ck_runtime_usage_tokens_nonnegative",
        ),
        CheckConstraint(
            "cost_micros IS NULL OR cost_micros >= 0",
            name="ck_runtime_usage_cost_nonnegative",
        ),
        CheckConstraint(
            "usage_status IN ('available','unavailable')",
            name="ck_runtime_usage_status",
        ),
        CheckConstraint(
            "cost_status IN ('priced','unpriced','unavailable')",
            name="ck_runtime_usage_cost_status",
        ),
        ForeignKeyConstraint(
            ["attempt_id"], ["agent_run_attempts.id"], ondelete="CASCADE",
            name="fk_runtime_usage_attempt",
        ),
        Index("ix_runtime_usage_tenant_observed", "tenant_id", "observed_at"),
        Index("ix_runtime_usage_run_kind", "run_id", "kind"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    attempt_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_event_id: Mapped[str] = mapped_column(String(160), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    backend: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cache_read_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cache_write_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cost_micros: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    upstream_request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    usage_status: Mapped[str] = mapped_column(String(24), nullable=False, default="unavailable")
    cost_status: Mapped[str] = mapped_column(String(24), nullable=False, default="unpriced")
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
