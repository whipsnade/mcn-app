"""add task retry idempotency fields

Revision ID: 0009_task_retry_idempotency
Revises: 0008_candidate_pool_export
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0009_task_retry_idempotency"
down_revision: str | None = "0008_candidate_pool_export"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("analysis_tasks", sa.Column("retry_of_task_id", sa.String(36), nullable=True))
    op.add_column("analysis_tasks", sa.Column("retry_key", sa.String(128), nullable=True))
    op.create_unique_constraint("uq_analysis_tasks_retry_key", "analysis_tasks", ["retry_key"])


def downgrade() -> None:
    op.drop_constraint("uq_analysis_tasks_retry_key", "analysis_tasks", type_="unique")
    op.drop_column("analysis_tasks", "retry_key")
    op.drop_column("analysis_tasks", "retry_of_task_id")
