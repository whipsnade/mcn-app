"""add task replan state

Revision ID: 0007_task_replan_state
Revises: 0006_optional_campaign_name
"""

from alembic import op
import sqlalchemy as sa


revision = "0007_task_replan_state"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("analysis_tasks", sa.Column("replan_json", sa.JSON(), nullable=True))
    op.add_column(
        "analysis_tasks",
        sa.Column("replan_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("analysis_tasks", "replan_count")
    op.drop_column("analysis_tasks", "replan_json")
