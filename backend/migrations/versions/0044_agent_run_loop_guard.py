"""0044 持久化跨 Attempt loop guard。"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0044_agent_run_loop_guard"
down_revision: str | None = "0043_billing_downgrade_guard"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column("loop_guard_json", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_runs", "loop_guard_json")
