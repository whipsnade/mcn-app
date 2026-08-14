"""Harden runtime usage lineage and counter constraints."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0041_runtime_usage_constraints"
down_revision: str | None = "0040_tenant_billing_usage"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    for column in (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "cost_micros",
    ):
        op.alter_column(
            "runtime_usage_records",
            column,
            existing_type=sa.Integer(),
            type_=sa.BigInteger(),
            existing_nullable=True,
        )
    op.create_foreign_key(
        "fk_runtime_usage_attempt",
        "runtime_usage_records",
        "agent_run_attempts",
        ["attempt_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_runtime_usage_upstream_request",
        "runtime_usage_records",
        ["tenant_id", "kind", "upstream_request_id"],
    )
    op.create_check_constraint(
        "ck_runtime_usage_kind",
        "runtime_usage_records",
        "kind IN ('model','mcp')",
    )
    op.create_check_constraint(
        "ck_runtime_usage_backend",
        "runtime_usage_records",
        "backend IN ('current','pi')",
    )
    op.create_check_constraint(
        "ck_runtime_usage_tokens_nonnegative",
        "runtime_usage_records",
        "(input_tokens IS NULL OR input_tokens >= 0) AND "
        "(output_tokens IS NULL OR output_tokens >= 0) AND "
        "(cache_read_tokens IS NULL OR cache_read_tokens >= 0) AND "
        "(cache_write_tokens IS NULL OR cache_write_tokens >= 0)",
    )
    op.create_check_constraint(
        "ck_runtime_usage_cost_nonnegative",
        "runtime_usage_records",
        "cost_micros IS NULL OR cost_micros >= 0",
    )
    op.create_check_constraint(
        "ck_runtime_usage_status",
        "runtime_usage_records",
        "usage_status IN ('available','unavailable')",
    )
    op.create_check_constraint(
        "ck_runtime_usage_cost_status",
        "runtime_usage_records",
        "cost_status IN ('priced','unpriced','unavailable')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_runtime_usage_cost_status", "runtime_usage_records", type_="check")
    op.drop_constraint("ck_runtime_usage_status", "runtime_usage_records", type_="check")
    op.drop_constraint("ck_runtime_usage_cost_nonnegative", "runtime_usage_records", type_="check")
    op.drop_constraint("ck_runtime_usage_tokens_nonnegative", "runtime_usage_records", type_="check")
    op.drop_constraint("ck_runtime_usage_backend", "runtime_usage_records", type_="check")
    op.drop_constraint("ck_runtime_usage_kind", "runtime_usage_records", type_="check")
    op.drop_constraint("uq_runtime_usage_upstream_request", "runtime_usage_records", type_="unique")
    op.drop_constraint("fk_runtime_usage_attempt", "runtime_usage_records", type_="foreignkey")
    for column in (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "cost_micros",
    ):
        op.alter_column(
            "runtime_usage_records",
            column,
            existing_type=sa.BigInteger(),
            type_=sa.Integer(),
            existing_nullable=True,
        )
