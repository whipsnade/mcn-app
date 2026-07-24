"""Add goal/artifact infrastructure tables and extend analysis_reports."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# 注：revision 需 ≤32 字符（alembic_version.version_num 为 VARCHAR(32)）。
revision: str = "0022_goal_artifact_infra"
down_revision: str | None = "0021_favorite_platform_uid"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_goals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "task_id",
            sa.String(36),
            sa.ForeignKey("analysis_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("goal_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column(
            "depends_on_goal_id",
            sa.String(36),
            sa.ForeignKey("task_goals.id"),
            nullable=True,
        ),
        sa.Column("params_json", sa.JSON(), nullable=False),
        sa.Column("trajectory_json", sa.JSON(), nullable=True),
        sa.Column("result_summary_json", sa.JSON(), nullable=True),
        sa.Column("warning_code", sa.String(64), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("task_id", "sequence", name="uq_task_goals_task_sequence"),
    )
    # kol_selection_sets 先于 task_artifacts 创建：task_artifacts 有指向它的外键。
    op.create_table(
        "kol_selection_sets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(36),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("task_id", sa.String(36), nullable=True),
        sa.Column("goal_id", sa.String(36), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False, server_default=""),
        sa.Column("scope_json", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("session_id", "version", name="uq_kol_selection_sets_session_version"),
    )
    op.create_table(
        "task_artifacts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(36),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("task_id", sa.String(36), nullable=True),
        sa.Column(
            "goal_id",
            sa.String(36),
            sa.ForeignKey("task_goals.id"),
            nullable=True,
        ),
        sa.Column("artifact_key", sa.String(191), nullable=False),
        sa.Column("artifact_type", sa.String(48), nullable=False),
        sa.Column("title", sa.String(200), nullable=False, server_default=""),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "report_id",
            sa.String(36),
            sa.ForeignKey("analysis_reports.id"),
            nullable=True,
        ),
        sa.Column(
            "selection_set_id",
            sa.String(36),
            sa.ForeignKey("kol_selection_sets.id"),
            nullable=True,
        ),
        sa.Column("scope_json", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("artifact_key", name="uq_task_artifacts_artifact_key"),
        sa.Index("ix_task_artifacts_session_type", "session_id", "artifact_type"),
    )
    op.create_table(
        "kol_selection_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "selection_set_id",
            sa.String(36),
            sa.ForeignKey("kol_selection_sets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("kol_uid", sa.String(128), nullable=False),
        sa.Column("nickname", sa.String(200), nullable=False, server_default=""),
        sa.Column("followers", sa.BigInteger(), nullable=True),
        sa.Column("city", sa.String(64), nullable=True),
        sa.Column("profile_url", sa.String(512), nullable=True),
        sa.Column("fields_json", sa.JSON(), nullable=False),
        sa.Column("score_json", sa.JSON(), nullable=False),
        sa.Column("source_tool", sa.String(128), nullable=False, server_default=""),
        sa.Column("first_task_id", sa.String(36), nullable=False, server_default=""),
        sa.Column("last_task_id", sa.String(36), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "selection_set_id",
            "platform",
            "kol_uid",
            name="uq_kol_selection_items_set_platform_uid",
        ),
        sa.Index("ix_kol_selection_items_user", "user_id"),
    )
    op.create_table(
        "artifact_read_states",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column(
            "session_id",
            sa.String(36),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("module_key", sa.String(32), nullable=False),
        sa.Column("last_seen_artifact_id", sa.String(36), nullable=True),
        sa.Column("seen_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "user_id",
            "session_id",
            "module_key",
            name="uq_artifact_read_states_user_session_module",
        ),
    )
    op.create_table(
        "user_brand_profiles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("brand_name", sa.String(100), nullable=False),
        # 非默认行存 NULL：MySQL 唯一索引允许多个 NULL，保证每用户最多一个默认品牌。
        sa.Column("is_default", sa.Boolean(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("user_id", "brand_name", name="uq_user_brand_profiles_user_brand"),
        sa.UniqueConstraint("user_id", "is_default", name="uq_user_brand_profiles_user_default"),
    )
    op.add_column(
        "analysis_reports",
        sa.Column(
            "report_type",
            sa.String(32),
            nullable=False,
            server_default="kol_analysis",
        ),
    )
    op.add_column("analysis_reports", sa.Column("scope_json", sa.JSON(), nullable=True))
    op.drop_constraint(
        "uq_analysis_reports_session_version", "analysis_reports", type_="unique"
    )
    op.create_unique_constraint(
        "uq_analysis_reports_session_type_version",
        "analysis_reports",
        ["session_id", "report_type", "version"],
    )
    op.add_column("mcp_calls", sa.Column("goal_id", sa.String(36), nullable=True))


def downgrade() -> None:
    op.drop_column("mcp_calls", "goal_id")
    op.drop_constraint(
        "uq_analysis_reports_session_type_version", "analysis_reports", type_="unique"
    )
    op.create_unique_constraint(
        "uq_analysis_reports_session_version", "analysis_reports", ["session_id", "version"]
    )
    op.drop_column("analysis_reports", "scope_json")
    op.drop_column("analysis_reports", "report_type")
    op.drop_table("user_brand_profiles")
    op.drop_table("artifact_read_states")
    op.drop_table("kol_selection_items")
    op.drop_table("task_artifacts")
    op.drop_table("kol_selection_sets")
    op.drop_table("task_goals")
