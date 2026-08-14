"""Add agent_artifact_read_states table and lineage_snapshot_json column.

Agent 已读水位从旧 ``artifact_read_states``（session_id FK 指向旧 ``sessions``，
新系统不再写入旧 sessions，首写必触发 FK 1452）切换到独立的
``agent_artifact_read_states``（session_id FK 指向 ``agent_sessions``）。旧表保持
不动，仅供旧应用版本回滚使用；不迁移旧水位数据。``agent_artifact_versions`` 新增
``lineage_snapshot_json``（发布时冻结的 Evidence 传递闭包审计快照，旧 Version 为
NULL；写入逻辑由后续任务落地）。
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0028_agent_artifact_read_states"
down_revision: str | None = "0027_agent_runtime_v3"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_artifact_read_states",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column(
            "session_id",
            sa.String(36),
            sa.ForeignKey("agent_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("module", sa.String(32), nullable=False),
        sa.Column("last_seen_sequence", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "user_id",
            "session_id",
            "module",
            name="uq_agent_artifact_read_states_user_session_module",
        ),
    )
    op.add_column(
        "agent_artifact_versions",
        sa.Column("lineage_snapshot_json", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_artifact_versions", "lineage_snapshot_json")
    op.drop_table("agent_artifact_read_states")
