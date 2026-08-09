"""Create the tenant wallet/quota ledger and register runtime usage records."""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

from alembic import op
import sqlalchemy as sa


revision: str = "0040_tenant_billing_usage"
down_revision: str | None = "0039a_pi_session_mutex_backfill"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tenant_wallets",
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("balance", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reserved", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tenant_id"),
        sa.CheckConstraint("balance >= 0", name="ck_tenant_wallet_balance_nonnegative"),
        sa.CheckConstraint("reserved >= 0", name="ck_tenant_wallet_reserved_nonnegative"),
    )
    op.create_table(
        "tenant_wallet_transactions",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=True),
        sa.Column("run_id", sa.String(36), nullable=True),
        sa.Column("tool_call_id", sa.String(36), nullable=True),
        sa.Column("internal_tool_name", sa.String(128), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("balance_delta", sa.Integer(), nullable=False),
        sa.Column("reserved_delta", sa.Integer(), nullable=False),
        sa.Column("balance_after", sa.Integer(), nullable=False),
        sa.Column("reserved_after", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("reference_type", sa.String(48), nullable=True),
        sa.Column("reference_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_tenant_wallet_tx_idempotency"),
        sa.Index("ix_tenant_wallet_tx_tenant_created", "tenant_id", "created_at"),
        sa.Index("ix_tenant_wallet_tx_reference", "reference_type", "reference_id"),
    )
    op.create_table(
        "tenant_user_quota_policies",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("period", sa.String(16), nullable=False, server_default="monthly"),
        sa.Column("points_limit", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "user_id", "period", name="uq_tenant_user_quota_policy"),
        sa.CheckConstraint("period = 'monthly'", name="ck_tenant_user_quota_period"),
        sa.CheckConstraint("points_limit >= 0", name="ck_tenant_user_quota_limit_nonnegative"),
        sa.CheckConstraint("status IN ('active','disabled')", name="ck_tenant_user_quota_status"),
        sa.Index("ix_tenant_user_quota_policy_user_status", "user_id", "status"),
    )
    op.create_table(
        "tenant_user_quota_usage",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("period_start", sa.DateTime(), nullable=False),
        sa.Column("period_end", sa.DateTime(), nullable=False),
        sa.Column("spent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reserved", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "user_id", "period_start", "period_end",
            name="uq_tenant_user_quota_usage_period",
        ),
        sa.CheckConstraint("spent >= 0", name="ck_tenant_user_quota_spent_nonnegative"),
        sa.CheckConstraint("reserved >= 0", name="ck_tenant_user_quota_reserved_nonnegative"),
        sa.Index("ix_tenant_user_quota_usage_user_period", "user_id", "period_start", "period_end"),
    )
    op.create_table(
        "tenant_billing_migration_validations",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("source_wallet_user_id", sa.String(36), nullable=False),
        sa.Column("source_balance", sa.Integer(), nullable=False),
        sa.Column("source_reserved", sa.Integer(), nullable=False),
        sa.Column("copied_balance", sa.Integer(), nullable=False),
        sa.Column("copied_reserved", sa.Integer(), nullable=False),
        sa.Column("source_transaction_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_wallet_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", name="uq_tenant_billing_migration_tenant"),
    )
    op.create_table(
        "runtime_usage_records",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("attempt_id", sa.String(36), nullable=False),
        sa.Column("source_event_id", sa.String(160), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("backend", sa.String(16), nullable=False),
        sa.Column("provider", sa.String(64), nullable=True),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_micros", sa.Integer(), nullable=True),
        sa.Column("currency", sa.String(8), nullable=True),
        sa.Column("upstream_request_id", sa.String(128), nullable=True),
        sa.Column("usage_status", sa.String(24), nullable=False, server_default="unavailable"),
        sa.Column("cost_status", sa.String(24), nullable=False, server_default="unpriced"),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "attempt_id", "source_event_id", "kind", name="uq_runtime_usage_event"),
        sa.Index("ix_runtime_usage_tenant_observed", "tenant_id", "observed_at"),
        sa.Index("ix_runtime_usage_run_kind", "run_id", "kind"),
    )

    connection = op.get_bind()
    now = datetime.now(UTC).replace(tzinfo=None)
    wallets = connection.execute(
        sa.text(
            "SELECT w.user_id, m.tenant_id, w.balance, w.reserved "
            "FROM wallets w JOIN tenant_memberships m ON m.user_id = w.user_id"
        )
    ).mappings().all()
    orphan_wallets = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM wallets w "
            "LEFT JOIN tenant_memberships m ON m.user_id = w.user_id "
            "WHERE m.user_id IS NULL"
        )
    ).scalar_one()
    if orphan_wallets:
        raise RuntimeError("tenant_billing_wallet_orphan")
    orphan_transactions = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM wallet_transactions t "
            "LEFT JOIN wallets w ON w.user_id = t.user_id "
            "LEFT JOIN tenant_memberships m ON m.user_id = t.user_id "
            "WHERE w.user_id IS NULL OR m.user_id IS NULL"
        )
    ).scalar_one()
    if orphan_transactions:
        raise RuntimeError("tenant_billing_transaction_orphan")
    for row in wallets:
        reserved_from_ledger = connection.execute(
            sa.text(
                "SELECT COALESCE(SUM(reserved_delta), 0) "
                "FROM wallet_transactions WHERE user_id = :user_id"
            ),
            {"user_id": row["user_id"]},
        ).scalar_one()
        if reserved_from_ledger != row["reserved"]:
            raise RuntimeError("tenant_billing_reserved_mismatch")
        transaction_count = connection.execute(
            sa.text("SELECT COUNT(*) FROM wallet_transactions WHERE user_id = :user_id"),
            {"user_id": row["user_id"]},
        ).scalar_one()
        connection.execute(
            sa.text(
                "INSERT INTO tenant_wallets "
                "(tenant_id,balance,reserved,version,updated_at) "
                "VALUES (:tenant_id,:balance,:reserved,0,:updated_at)"
            ),
            {
                "tenant_id": row["tenant_id"],
                "balance": row["balance"],
                "reserved": row["reserved"],
                "updated_at": now,
            },
        )
        transactions = connection.execute(
            sa.text("SELECT * FROM wallet_transactions WHERE user_id = :user_id ORDER BY created_at, id"),
            {"user_id": row["user_id"]},
        ).mappings().all()
        for transaction in transactions:
            connection.execute(
                sa.text(
                    "INSERT INTO tenant_wallet_transactions "
                    "(id,tenant_id,user_id,run_id,tool_call_id,internal_tool_name,kind,balance_delta,reserved_delta,"
                    "balance_after,reserved_after,idempotency_key,reference_type,reference_id,created_at) "
                    "VALUES (:id,:tenant_id,:user_id,NULL,NULL,NULL,:kind,:balance_delta,:reserved_delta,"
                    ":balance_after,:reserved_after,:idempotency_key,:reference_type,:reference_id,:created_at)"
                ),
                {**transaction, "tenant_id": row["tenant_id"]},
            )
        connection.execute(
            sa.text(
                "INSERT INTO tenant_billing_migration_validations "
                "(id,tenant_id,source_wallet_user_id,source_balance,source_reserved,copied_balance,"
                "copied_reserved,source_transaction_count,status,created_at) "
                "VALUES (:id,:tenant_id,:user_id,:balance,:reserved,:balance,:reserved,:count,'ok',:created_at)"
            ),
            {
                "id": str(uuid4()),
                "tenant_id": row["tenant_id"],
                "user_id": row["user_id"],
                "balance": row["balance"],
                "reserved": row["reserved"],
                "count": transaction_count,
                "created_at": now,
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO tenant_user_quota_policies "
                "(id,tenant_id,user_id,period,points_limit,status,created_at,updated_at) "
                "VALUES (:id,:tenant_id,:user_id,'monthly',1000,'active',:created_at,:updated_at)"
            ),
            {
                "id": str(uuid4()),
                "tenant_id": row["tenant_id"],
                "user_id": row["user_id"],
                "created_at": now,
                "updated_at": now,
            },
        )


def downgrade() -> None:
    op.drop_table("runtime_usage_records")
    op.drop_table("tenant_billing_migration_validations")
    op.drop_table("tenant_user_quota_usage")
    op.drop_table("tenant_user_quota_policies")
    op.drop_table("tenant_wallet_transactions")
    op.drop_table("tenant_wallets")
