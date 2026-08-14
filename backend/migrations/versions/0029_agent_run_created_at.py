"""Add created_at to agent_runs for stable session-detail run ordering.

Session detail runs were ordered by ``AgentRun.id`` (random uuid4), so the
frontend anchored the "latest run" to ``runs.at(-1)`` of a randomly ordered
list and could restore any historical run (including kol_detail helper runs)
after a reload. ``created_at`` gives a chronological key; id stays as the
tie-break. Existing rows are backfilled from ``started_at`` (first claim
time), falling back to the migration timestamp.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0029_agent_run_created_at"
down_revision: str | None = "0028_agent_artifact_read_states"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("created_at", sa.DateTime(), nullable=True))
    # 存量回填：优先 started_at（首次 claim 时间），缺失时用迁移时刻兜底。
    op.execute("UPDATE agent_runs SET created_at = COALESCE(started_at, NOW())")
    op.alter_column("agent_runs", "created_at", existing_type=sa.DateTime(), nullable=False)


def downgrade() -> None:
    op.drop_column("agent_runs", "created_at")
