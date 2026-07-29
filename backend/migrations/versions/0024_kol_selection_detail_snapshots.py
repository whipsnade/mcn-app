"""Add versioned KOL detail and trend snapshots."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0024_kol_detail_snapshots"
down_revision: str | None = "0023_goal_artifact_backfill"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "kol_selection_detail_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "selection_set_id",
            sa.String(36),
            sa.ForeignKey("kol_selection_sets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("kol_uid", sa.String(128), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("ranking_interaction", sa.Float(), nullable=False),
        sa.Column("scope_status_json", sa.JSON(), nullable=False),
        sa.Column("facts_json", sa.JSON(), nullable=False),
        sa.Column("trend_points_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("`rank` BETWEEN 1 AND 20", name="ck_kol_detail_snapshot_rank"),
        sa.UniqueConstraint(
            "selection_set_id",
            "platform",
            "kol_uid",
            name="uq_kol_detail_snapshot_set_platform_uid",
        ),
    )
    op.create_index(
        "ix_kol_detail_snapshot_set_rank",
        "kol_selection_detail_snapshots",
        ["selection_set_id", "rank"],
    )


def downgrade() -> None:
    op.drop_index("ix_kol_detail_snapshot_set_rank", table_name="kol_selection_detail_snapshots")
    op.drop_table("kol_selection_detail_snapshots")
