"""Create KOL candidate and BI reporting tables."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "kols",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("platform_account_id", sa.String(128), nullable=False),
        sa.Column("normalized_profile_url", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "platform", "platform_account_id", name="uq_kols_platform_account"
        ),
    )

    op.create_table(
        "kol_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kol_id", sa.String(36), nullable=False),
        sa.Column("source_mcp_call_id", sa.String(36), nullable=True),
        sa.Column("normalized_json", sa.JSON(), nullable=False),
        sa.Column("collected_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["kol_id"], ["kols.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_mcp_call_id"], ["mcp_calls.id"], ondelete="SET NULL"
        ),
    )
    op.create_index(
        "ix_kol_snapshots_kol_collected",
        "kol_snapshots",
        ["kol_id", "collected_at"],
    )

    op.create_table(
        "task_candidates",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(36), nullable=False),
        sa.Column("kol_id", sa.String(36), nullable=False),
        sa.Column("snapshot_id", sa.String(36), nullable=False),
        sa.Column("candidate_version", sa.Integer(), nullable=False),
        sa.Column("total_score", sa.Numeric(6, 3), nullable=False),
        sa.Column("score_breakdown_json", sa.JSON(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("matched_conditions_json", sa.JSON(), nullable=False),
        sa.Column("risk_flags_json", sa.JSON(), nullable=False),
        sa.Column("recommendation_text", sa.Text(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["analysis_tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["kol_id"], ["kols.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["snapshot_id"], ["kol_snapshots.id"]),
        sa.UniqueConstraint(
            "task_id", "candidate_version", "kol_id", name="uq_task_candidates_version_kol"
        ),
    )
    op.create_index(
        "ix_task_candidates_task_version_rank",
        "task_candidates",
        ["task_id", "candidate_version", "rank"],
    )

    op.create_table(
        "bi_reports",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(36), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("candidate_version", sa.Integer(), nullable=False),
        sa.Column("report_version", sa.Integer(), nullable=False),
        sa.Column("chart_data_json", sa.JSON(), nullable=False),
        sa.Column("conclusion_text", sa.Text(), nullable=True),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["analysis_tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("task_id", "report_version", name="uq_bi_reports_task_version"),
    )
    op.create_index(
        "ix_bi_reports_session_created", "bi_reports", ["session_id", "created_at"]
    )

    op.create_table(
        "user_kol_favorites",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("kol_id", sa.String(36), nullable=False),
        sa.Column("note", sa.String(500), nullable=True),
        sa.Column("source_task_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["kol_id"], ["kols.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_task_id"], ["analysis_tasks.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint(
            "user_id", "kol_id", name="uq_user_kol_favorites_user_kol"
        ),
    )
    op.create_index(
        "ix_user_kol_favorites_user_created",
        "user_kol_favorites",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("user_kol_favorites")
    op.drop_table("bi_reports")
    op.drop_table("task_candidates")
    op.drop_table("kol_snapshots")
    op.drop_table("kols")
