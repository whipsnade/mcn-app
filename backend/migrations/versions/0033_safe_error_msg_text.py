"""agent_tool_calls.safe_error_message 改 MEDIUMTEXT：结构化反馈 JSON 可超 VARCHAR(500)。

Gate B 审查：ToolFailureFeedback JSON 含参数摘要 + 建议动作，容易超过 500
字符，导致 finalize 事务写入失败、调用行残留 running/预留态。
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy.dialects.mysql import MEDIUMTEXT

import sqlalchemy as sa


revision: str = "0033_safe_error_msg_text"
down_revision: str | None = "0032_evidence_run_id_nullable"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "agent_tool_calls",
        "safe_error_message",
        existing_type=sa.String(500),
        type_=MEDIUMTEXT(),
        existing_nullable=True,
    )


def downgrade() -> None:
    # 截断超长值后再改回 VARCHAR(500)，否则 MySQL 拒绝截断类型收窄。
    op.execute(
        "UPDATE agent_tool_calls SET safe_error_message = "
        "LEFT(safe_error_message, 497) WHERE CHAR_LENGTH(safe_error_message) > 500"
    )
    op.alter_column(
        "agent_tool_calls",
        "safe_error_message",
        existing_type=MEDIUMTEXT(),
        type_=sa.String(500),
        existing_nullable=True,
    )
