"""Add a monotonic per-session task creation sequence."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0012_task_creation_order"
down_revision: str | None = "0011_session_soft_delete"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def _analysis_task_index_names() -> set[str]:
    bind = op.get_bind()
    return {
        item["name"]
        for item in sa.inspect(bind).get_indexes("analysis_tasks")
    }


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
    # A downgrade leaves a session_id-only index behind because MySQL requires
    # a leading-column index for the analysis_tasks.session_id foreign key.
    # Remove that compatibility index after the replacement is in place.
    if "ix_analysis_tasks_session_id" in _analysis_task_index_names():
        op.drop_index("ix_analysis_tasks_session_id", table_name="analysis_tasks")


def downgrade() -> None:
    # Keep a leading session_id index while removing the composite index; this
    # is required by MySQL 1553 for the existing session_id foreign key.
    if "ix_analysis_tasks_session_id" not in _analysis_task_index_names():
        op.create_index(
            "ix_analysis_tasks_session_id",
            "analysis_tasks",
            ["session_id"],
        )
    if "ix_analysis_tasks_session_creation_order" in _analysis_task_index_names():
        op.drop_index(
            "ix_analysis_tasks_session_creation_order",
            table_name="analysis_tasks",
        )
    op.drop_column("analysis_tasks", "creation_order")
