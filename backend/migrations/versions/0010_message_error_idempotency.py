"""add idempotency key for persisted task error messages

Revision ID: 0010_message_error_idempotency
Revises: 0009_task_retry_idempotency
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0010_message_error_idempotency"
down_revision: str | None = "0009_task_retry_idempotency"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("error_idempotency_key", sa.String(128), nullable=True))
    op.create_unique_constraint(
        "uq_messages_error_idempotency_key", "messages", ["error_idempotency_key"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_messages_error_idempotency_key", "messages", type_="unique")
    op.drop_column("messages", "error_idempotency_key")
