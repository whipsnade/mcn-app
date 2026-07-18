"""Create analysis_reports table for free-form agent reports."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0015_analysis_reports"
down_revision: str | None = "0014_task_kind"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "analysis_reports",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "task_id",
            sa.String(36),
            sa.ForeignKey("analysis_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "session_id",
            sa.String(36),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False, server_default=""),
        sa.Column("blocks_json", sa.JSON(), nullable=False),
        sa.Column("conclusion_text", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("task_id", "version", name="uq_analysis_reports_task_version"),
        sa.Index("ix_analysis_reports_session_created", "session_id", "created_at"),
    )


def downgrade() -> None:
    op.drop_table("analysis_reports")
