"""Backfill legacy kol selections/reports into goal/artifact tables."""

from collections.abc import Sequence

from alembic import op
from sqlalchemy.orm import Session

from app.artifacts.backfill import LEGACY_SET_TITLE, run_backfill_sync


# 注：revision 需 ≤32 字符（alembic_version.version_num 为 VARCHAR(32)）。
revision: str = "0023_goal_artifact_backfill"
down_revision: str | None = "0022_goal_artifact_infra"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    with Session(bind=op.get_bind()) as session:
        run_backfill_sync(session)
        session.commit()


def downgrade() -> None:
    # report_type 的兜底 UPDATE 不回滚（列本身保留默认值语义）。
    op.execute("DELETE FROM task_artifacts WHERE artifact_key LIKE 'legacy:%'")
    op.execute(
        "DELETE FROM kol_selection_items WHERE selection_set_id IN "
        f"(SELECT id FROM kol_selection_sets WHERE title = '{LEGACY_SET_TITLE}')"
    )
    op.execute(f"DELETE FROM kol_selection_sets WHERE title = '{LEGACY_SET_TITLE}'")
