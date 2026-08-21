"""0046 widen export cache identity for workbook layout-aware keys."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0046_workbook_export_cache_key"
down_revision: str | None = "0045_marketing_skill_registry"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "artifact_exports",
        "template_version",
        existing_type=sa.String(length=32),
        type_=sa.String(length=191),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "artifact_exports",
        "template_version",
        existing_type=sa.String(length=191),
        type_=sa.String(length=32),
        existing_nullable=False,
    )
