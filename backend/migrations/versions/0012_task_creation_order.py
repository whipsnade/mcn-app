"""Add a monotonic per-session task creation sequence."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0012_task_creation_order"
down_revision: str | None = "0011_session_soft_delete"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "analysis_tasks",
        sa.Column("creation_order", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "ix_analysis_tasks_session_creation_order",
        "analysis_tasks",
        ["session_id", "creation_order"],
    )


def downgrade() -> None:
    op.drop_index("ix_analysis_tasks_session_creation_order", table_name="analysis_tasks")
    op.drop_column("analysis_tasks", "creation_order")
