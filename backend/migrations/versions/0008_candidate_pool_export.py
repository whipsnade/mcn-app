"""persist complete candidate pools for export

Revision ID: 0008_candidate_pool_export
Revises: 0007_task_replan_state
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0008_candidate_pool_export"
down_revision: str | None = "0007_task_replan_state"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_candidate_pools",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(36), nullable=False),
        sa.Column("pool_version", sa.Integer(), nullable=False),
        sa.Column("field_contract_version", sa.String(32), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("evidence_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["analysis_tasks.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "task_id", "pool_version", name="uq_task_candidate_pools_task_version"
        ),
    )
    op.create_index(
        "ix_task_candidate_pools_task_version",
        "task_candidate_pools",
        ["task_id", "pool_version"],
    )
    op.create_table(
        "task_candidate_pool_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("pool_id", sa.String(36), nullable=False),
        sa.Column("kol_id", sa.String(36), nullable=False),
        sa.Column("snapshot_id", sa.String(36), nullable=False),
        sa.Column("full_rank", sa.Integer(), nullable=False),
        sa.Column("is_shortlisted", sa.Boolean(), nullable=False),
        sa.Column("total_score", sa.Numeric(6, 3), nullable=False),
        sa.Column("score_breakdown_json", sa.JSON(), nullable=False),
        sa.Column("risk_flags_json", sa.JSON(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["pool_id"], ["task_candidate_pools.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["kol_id"], ["kols.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["snapshot_id"], ["kol_snapshots.id"]),
        sa.UniqueConstraint("pool_id", "kol_id", name="uq_task_candidate_pool_items_pool_kol"),
    )
    op.create_index(
        "ix_task_candidate_pool_items_pool_rank",
        "task_candidate_pool_items",
        ["pool_id", "full_rank"],
    )


def downgrade() -> None:
    op.drop_table("task_candidate_pool_items")
    op.drop_index("ix_task_candidate_pools_task_version", table_name="task_candidate_pools")
    op.drop_table("task_candidate_pools")
