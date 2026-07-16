"""add session soft deletion

Revision ID: 0011_session_soft_delete
Revises: 0010_message_error_idempotency
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0011_session_soft_delete"
down_revision: str | None = "0010_message_error_idempotency"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("deleted_at", sa.DateTime(), nullable=True))
    op.create_index(
        "ix_sessions_user_deleted_last_accessed",
        "sessions",
        ["user_id", "deleted_at", "last_accessed_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_sessions_user_deleted_last_accessed", table_name="sessions")
    op.drop_column("sessions", "deleted_at")
