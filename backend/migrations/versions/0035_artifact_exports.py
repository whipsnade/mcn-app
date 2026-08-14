"""0035_artifact_exports: Excel 导出缓存表（Gate C Task 6）。

同一 (artifact_version_id, template_version) 只构建一次；building/ready/failed
三态 + 唯一约束支撑并发协调与失败重试。模板版本来自 Artifact 的
schema_version（当前 v3 族各自固定）。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035_artifact_exports"
down_revision: str | None = "0034_dispatch_count"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "artifact_exports",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "artifact_version_id",
            sa.String(36),
            sa.ForeignKey("agent_artifact_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("template_version", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("filename", sa.String(255), nullable=True),
        sa.Column("storage_key", sa.String(255), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "artifact_version_id",
            "template_version",
            name="uq_artifact_exports_version_template",
        ),
    )
    op.create_index(
        "ix_artifact_exports_artifact_version_id",
        "artifact_exports",
        ["artifact_version_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_artifact_exports_artifact_version_id", table_name="artifact_exports")
    op.drop_table("artifact_exports")
