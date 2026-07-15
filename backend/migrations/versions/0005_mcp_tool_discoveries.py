"""Persist quarantined MCP tool discoveries."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


SERVICE_SLUG_CHECK = (
    "service_slug IN ('insight-cube-mcp', 'social-grow-mcp', "
    "'social-grow-content-mcp', 'aktools-mcp', 'bilibili-mcp')"
)
REVIEW_STATUS_CHECK = "review_status IN ('quarantined', 'approved', 'rejected')"


def upgrade() -> None:
    op.create_table(
        "mcp_tool_discoveries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("service_slug", sa.String(64), nullable=False),
        sa.Column("remote_name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("input_schema_json", sa.JSON(), nullable=False),
        sa.Column("output_schema_json", sa.JSON(), nullable=True),
        sa.Column("discovery_digest", sa.String(64), nullable=False),
        sa.Column("review_status", sa.String(32), nullable=False),
        sa.Column("discovered_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            SERVICE_SLUG_CHECK,
            name="ck_mcp_tool_discoveries_service_slug",
        ),
        sa.CheckConstraint(
            REVIEW_STATUS_CHECK,
            name="ck_mcp_tool_discoveries_review_status",
        ),
        sa.UniqueConstraint(
            "service_slug",
            "remote_name",
            name="uq_mcp_tool_discoveries_service_remote",
        ),
    )
    op.create_index(
        "ix_mcp_tool_discoveries_service_status",
        "mcp_tool_discoveries",
        ["service_slug", "review_status"],
    )


def downgrade() -> None:
    op.drop_table("mcp_tool_discoveries")
