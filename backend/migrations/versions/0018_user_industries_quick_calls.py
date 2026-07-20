"""Add users.industries (backfilled with ["美食"]) and the quick_mcp_calls ledger."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0018_user_industries_quick_calls"
down_revision: str | None = "0017_admin_audit_logs"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # MySQL 不允许 JSON 列直接声明字面量默认值：先加可空列、回填、再置 NOT NULL。
    op.add_column("users", sa.Column("industries", sa.JSON(), nullable=True))
    op.execute("UPDATE users SET industries = JSON_ARRAY('美食') WHERE industries IS NULL")
    op.alter_column("users", "industries", existing_type=sa.JSON(), nullable=False)

    op.create_table(
        "quick_mcp_calls",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("feature", sa.String(24), nullable=False),
        sa.Column("internal_tool_name", sa.String(128), nullable=False),
        sa.Column("arguments_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("points_cost", sa.Integer(), nullable=False),
        sa.Column(
            "reserve_transaction_id",
            sa.String(36),
            sa.ForeignKey("wallet_transactions.id"),
        ),
        sa.Column(
            "settlement_transaction_id",
            sa.String(36),
            sa.ForeignKey("wallet_transactions.id"),
        ),
        sa.Column("error_type", sa.String(64)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime()),
        sa.Index("ix_quick_mcp_calls_user_created", "user_id", "created_at"),
    )


def downgrade() -> None:
    op.drop_table("quick_mcp_calls")
    op.drop_column("users", "industries")
