"""Allow sessions without an activity or campaign name."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "sessions",
        "campaign_name",
        existing_type=sa.String(length=120),
        existing_nullable=False,
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "sessions",
        "campaign_name",
        existing_type=sa.String(length=120),
        existing_nullable=True,
        nullable=False,
    )
