"""Add structured payload snapshot columns to analysis_reports."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0026_brand_report_v2_payload"
down_revision: str | None = "0025_kol_selection_detail_views"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("analysis_reports", sa.Column("payload_json", sa.JSON(), nullable=True))
    op.add_column("analysis_reports", sa.Column("template_version", sa.String(32), nullable=True))


def downgrade() -> None:
    op.drop_column("analysis_reports", "template_version")
    op.drop_column("analysis_reports", "payload_json")
