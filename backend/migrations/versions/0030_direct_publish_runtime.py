"""直接发布运行记录：artifact_publish_attempts + versions.validation_json + confirmed_scope。

Reviewer 模型审批被确定性直接发布取代：每次发布尝试落
``artifact_publish_attempts``（idempotency_key 唯一幂等，状态机
validating/published/validation_failed/failed），校验快照随发布冻结到
``agent_artifact_versions.validation_json``；``memory_entries`` 类型白名单
加入 ``confirmed_scope``。downgrade 只移除本迁移创建的对象，不触碰
Review/reviewer 相关表。
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0030_direct_publish_runtime"
down_revision: str | None = "0029_agent_run_created_at"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "artifact_publish_attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("agent_runs.id"),
            nullable=False,
        ),
        sa.Column(
            "artifact_id",
            sa.String(36),
            sa.ForeignKey("agent_artifacts.id"),
            nullable=False,
        ),
        sa.Column(
            "draft_revision_id",
            sa.String(36),
            sa.ForeignKey("artifact_draft_revisions.id"),
            nullable=False,
        ),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("idempotency_key", sa.String(191), nullable=False),
        sa.Column("validation_json", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column(
            "published_version_id",
            sa.String(36),
            sa.ForeignKey("agent_artifact_versions.id"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_artifact_publish_attempts_idempotency"
        ),
        sa.CheckConstraint(
            "status IN ('validating','published','validation_failed','failed')",
            name="ck_artifact_publish_attempts_status",
        ),
    )
    op.add_column(
        "agent_artifact_versions",
        sa.Column("validation_json", sa.JSON(), nullable=True),
    )
    # memory_entries 类型白名单加入 confirmed_scope（MySQL 需重建 CHECK 约束）。
    op.drop_constraint("ck_memory_entries_type", "memory_entries", type_="check")
    op.create_check_constraint(
        "ck_memory_entries_type",
        "memory_entries",
        "memory_type IN ('run_summary','artifact_index','pending_question','confirmed_scope')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_memory_entries_type", "memory_entries", type_="check")
    op.create_check_constraint(
        "ck_memory_entries_type",
        "memory_entries",
        "memory_type IN ('run_summary','artifact_index','pending_question')",
    )
    op.drop_column("agent_artifact_versions", "validation_json")
    op.drop_table("artifact_publish_attempts")
