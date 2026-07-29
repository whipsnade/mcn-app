"""Add versioned KOL detail view cache."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0025_kol_selection_detail_views"
down_revision: str | None = "0024_kol_detail_snapshots"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "kol_selection_detail_views",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "selection_set_id",
            sa.String(36),
            sa.ForeignKey("kol_selection_sets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("kol_uid", sa.String(128), nullable=False),
        sa.Column("detail_json", sa.JSON(), nullable=False),
        sa.Column("posts_json", sa.JSON(), nullable=False),
        sa.Column("points_cost", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("posts_degraded", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "selection_set_id",
            "platform",
            "kol_uid",
            name="uq_kol_detail_view_set_platform_uid",
        ),
    )
    op.create_index(
        "ix_kol_detail_view_set_fetched",
        "kol_selection_detail_views",
        ["selection_set_id", "fetched_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_kol_detail_view_set_fetched", table_name="kol_selection_detail_views")
    op.drop_table("kol_selection_detail_views")
