"""Backfill the Session mutex introduced by the Pi Gateway control plane."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0039a_pi_session_mutex_backfill"
down_revision: str | None = "0039_pi_gateway_control_plane"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


_ACTIVE_STATUSES = ("queued", "running", "reviewing")


def upgrade() -> None:
    connection = op.get_bind()
    session_ids = connection.execute(sa.text("SELECT id, active_run_id FROM agent_sessions")).all()
    for session_id, existing_slot in session_ids:
        active_runs = connection.execute(
            sa.text(
                """
                SELECT id
                FROM agent_runs
                WHERE session_id = :session_id
                  AND run_kind = 'user'
                  AND visibility = 'user'
                  AND status IN :statuses
                ORDER BY created_at, id
                """
            ).bindparams(sa.bindparam("statuses", expanding=True)),
            {"session_id": session_id, "statuses": _ACTIVE_STATUSES},
        ).scalars().all()
        if len(active_runs) > 1:
            raise RuntimeError("pi_session_mutex_backfill_conflict")
        if existing_slot is not None and (not active_runs or existing_slot != active_runs[0]):
            raise RuntimeError("pi_session_mutex_backfill_conflict")
        if active_runs:
            connection.execute(
                sa.text(
                    "UPDATE agent_sessions SET active_run_id = :run_id WHERE id = :session_id"
                ),
                {"run_id": active_runs[0], "session_id": session_id},
            )


def downgrade() -> None:
    # The data backfill is intentionally retained until 0039 itself is
    # downgraded; clearing the mutex here would reopen concurrent Run creation.
    pass
