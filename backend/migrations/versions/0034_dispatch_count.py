"""agent_tool_calls.dispatch_count：指纹级外发计数（Gate B 最终审核 M1）。

definitely_not_sent 允许同指纹重试一次（总外发 ≤ 2），其他终态禁止重发。
dispatch_count 追踪每个 logical_call_id 的外发次数，使 prepare() 能区分
"首次失败可重试"与"已达上限阻止"。
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0034_dispatch_count"
down_revision: str | None = "0033_safe_error_msg_text"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_tool_calls",
        sa.Column(
            "dispatch_count",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )


def downgrade() -> None:
    # 危险降级保护（Gate B P1）：若存在 dispatch_count != 1 的调用行（发生过
    # 重试），直接 drop 列会静默重置计数，再次 upgrade 后可能允许第三次外发。
    bind = op.get_bind()
    unusual = bind.scalar(
        sa.text(
            "SELECT COUNT(*) FROM agent_tool_calls WHERE dispatch_count != 1"
        )
    )
    if unusual:
        raise AssertionError(
            f"refusing dangerous downgrade: {unusual} agent_tool_calls have "
            "dispatch_count != 1 (retried). Drain/terminate active runs before downgrade."
        )
    op.drop_column("agent_tool_calls", "dispatch_count")
