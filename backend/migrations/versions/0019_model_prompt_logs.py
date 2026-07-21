"""Add the model_prompt_logs prompt learning log table."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision: str = "0019_model_prompt_logs"
down_revision: str | None = "0018_user_industries_quick_calls"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_prompt_logs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("session_id", sa.String(36), nullable=True),
        sa.Column("task_id", sa.String(36), nullable=True),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("messages", mysql.MEDIUMTEXT(), nullable=False),
        sa.Column("response", mysql.MEDIUMTEXT(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Index(
            "ix_model_prompt_logs_purpose_status_created",
            "purpose",
            "status",
            "created_at",
        ),
        sa.Index("ix_model_prompt_logs_user_created", "user_id", "created_at"),
    )


def downgrade() -> None:
    op.drop_table("model_prompt_logs")
