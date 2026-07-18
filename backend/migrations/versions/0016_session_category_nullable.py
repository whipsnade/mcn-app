"""Allow sessions without a category for free-form agent conversations."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0016_session_category_nullable"
down_revision: str | None = "0015_analysis_reports"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "sessions",
        "category",
        existing_type=sa.String(100),
        nullable=True,
    )


def downgrade() -> None:
    # Legacy free-form rows would violate the old NOT NULL constraint; the
    # downgrade intentionally requires them to be removed first.
    op.alter_column(
        "sessions",
        "category",
        existing_type=sa.String(100),
        nullable=False,
    )
