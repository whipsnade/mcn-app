"""Create admin_audit_logs table for administrator action auditing."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0017_admin_audit_logs"
down_revision: str | None = "0016_session_category_nullable"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "admin_audit_logs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "admin_user_id",
            sa.String(36),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=False),
        sa.Column("detail_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Index("ix_admin_audit_logs_admin_created", "admin_user_id", "created_at"),
    )


def downgrade() -> None:
    op.drop_table("admin_audit_logs")
