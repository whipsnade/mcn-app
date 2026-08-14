"""evidence_items.run_id 可空：upload Evidence 在 Run 创建前落库。

用户上传文件在消息引用之前独立上传（``agent_uploads``），解析结果作为
upload Evidence 写入时还没有关联 Run，``evidence_items.run_id`` 必须可空。
``tool_call_id XOR upload_id`` 约束不变（0031）：MCP Evidence 仍带 run_id，
upload Evidence 的 run_id 为 NULL。
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0032_evidence_run_id_nullable"
down_revision: str | None = "0031_evidence_uploads"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "evidence_items",
        "run_id",
        existing_type=sa.String(36),
        nullable=True,
    )


def downgrade() -> None:
    # 回滚前清理 upload-only Evidence（run_id 为 NULL 的行），否则非空约束
    # 重建会校验既有行失败。
    op.execute("DELETE FROM evidence_items WHERE run_id IS NULL")
    op.alter_column(
        "evidence_items",
        "run_id",
        existing_type=sa.String(36),
        nullable=False,
    )
