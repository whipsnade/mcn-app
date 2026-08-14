"""0036_export_claim_token: 导出缓存 owner fencing（Gate C 复审）。

artifact_exports 增加 claim_token：认领 building 行时写入唯一 token，完成/失败
用「WHERE claim_token 匹配」的条件更新；租约超时被接管的僵尸构建方持旧 token
的更新影响 0 行，绝不覆盖新 owner 的结果。既有行 claim_token 为 NULL，下一次
认领/接管时写入。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036_export_claim_token"
down_revision: str | None = "0035_artifact_exports"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "artifact_exports",
        sa.Column("claim_token", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("artifact_exports", "claim_token")
