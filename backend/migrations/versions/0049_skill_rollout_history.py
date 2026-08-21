"""0049 retain the previous rollout state for exact Skill rollback."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0049_skill_rollout_history"
down_revision: str | None = "0048_marketing_skill_audited_baseline"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "skill_activations",
        sa.Column("previous_rollout_percent", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "ck_skill_activations_previous_rollout_percent",
        "skill_activations",
        "previous_rollout_percent IS NULL OR "
        "(previous_rollout_percent >= 0 AND previous_rollout_percent <= 100)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_skill_activations_previous_rollout_percent",
        "skill_activations",
        type_="check",
    )
    op.drop_column("skill_activations", "previous_rollout_percent")
