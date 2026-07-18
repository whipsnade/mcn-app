"""Add task kind for pipeline/agent execution mode routing."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0014_task_kind"
down_revision: str | None = "0013_task_create_idempotency"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # Existing rows are the fixed KOL pipeline; only new tasks can be "agent".
    op.add_column(
        "analysis_tasks",
        sa.Column("kind", sa.String(16), nullable=False, server_default="pipeline"),
    )


def downgrade() -> None:
    op.drop_column("analysis_tasks", "kind")
