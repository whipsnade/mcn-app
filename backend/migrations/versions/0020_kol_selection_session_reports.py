"""Add session_kol_selections and make analysis_reports session-scoped."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# 注：revision 需 ≤32 字符（alembic_version.version_num 为 VARCHAR(32)）。
revision: str = "0020_kol_selection_session_rpts"
down_revision: str | None = "0019_model_prompt_logs"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "session_kol_selections",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "session_id",
            sa.String(36),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
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
            "session_id",
            "platform",
            "kol_uid",
            name="uq_kol_selection_session_platform_uid",
        ),
        sa.Index("ix_kol_selection_user", "user_id"),
    )
    # 存量任务级报告 version 按任务编号，同一会话可能有多行 version=1，
    # 与新唯一约束冲突：先按 (session_id, created_at) 重编号。
    op.execute(
        """
        UPDATE analysis_reports r
        JOIN (
            SELECT id, ROW_NUMBER() OVER (
                PARTITION BY session_id ORDER BY created_at, id
            ) AS rn
            FROM analysis_reports
        ) t ON t.id = r.id
        SET r.version = t.rn
        """
    )
    op.alter_column("analysis_reports", "task_id", existing_type=sa.String(36), nullable=True)
    op.create_unique_constraint(
        "uq_analysis_reports_session_version", "analysis_reports", ["session_id", "version"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_analysis_reports_session_version", "analysis_reports", type_="unique")
    op.alter_column("analysis_reports", "task_id", existing_type=sa.String(36), nullable=False)
    op.drop_table("session_kol_selections")
