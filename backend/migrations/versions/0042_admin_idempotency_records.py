"""Persist admin write idempotency records (actor/action/key unique)."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0042_admin_idempotency_records"
down_revision: str | None = "0041_runtime_usage_constraints"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "admin_idempotency_records",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("actor_id", sa.String(36), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(48), nullable=False),
        sa.Column("target_id", sa.String(128), nullable=False),
        sa.Column("response_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "actor_id",
            "action",
            "idempotency_key",
            name="uq_admin_idempotency_actor_action_key",
        ),
        sa.Index("ix_admin_idempotency_created", "created_at"),
    )


def downgrade() -> None:
    op.drop_table("admin_idempotency_records")
