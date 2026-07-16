"""Persist request idempotency digests for task creation."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0013_task_create_idempotency"
down_revision: str | None = "0012_task_creation_order"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "analysis_tasks",
        sa.Column("idempotency_key_hash", sa.String(64), nullable=True),
    )
    op.add_column(
        "analysis_tasks",
        sa.Column("idempotency_payload_hash", sa.String(64), nullable=True),
    )
    # A unique index (rather than a table constraint) is supported by both
    # MySQL and SQLite. Nullable keys keep legacy/no-header requests unchanged.
    op.create_index(
        "uq_analysis_tasks_user_session_idempotency",
        "analysis_tasks",
        ["user_id", "session_id", "idempotency_key_hash"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_analysis_tasks_user_session_idempotency",
        table_name="analysis_tasks",
    )
    op.drop_column("analysis_tasks", "idempotency_payload_hash")
    op.drop_column("analysis_tasks", "idempotency_key_hash")
