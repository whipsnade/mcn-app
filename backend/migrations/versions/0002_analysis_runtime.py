"""Create analysis runtime persistence tables."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


SERVICE_SLUG_CHECK = (
    "service_slug IN ('insight-cube-mcp', 'social-grow-mcp', "
    "'social-grow-content-mcp', 'aktools-mcp', 'bilibili-mcp')"
)
MCP_CALL_STATUS_CHECK = (
    "status IN ('planned', 'reserved', 'running', 'succeeded', 'failed', "
    "'unknown', 'settled', 'released')"
)


def upgrade() -> None:
    op.create_table(
        "analysis_tasks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("trigger_message_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("plan_json", sa.JSON(), nullable=True),
        sa.Column("plan_version", sa.String(32), nullable=True),
        sa.Column("max_calls", sa.Integer(), nullable=False),
        sa.Column("estimated_points", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.String(500), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(), nullable=True),
        sa.Column("lease_owner", sa.String(64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["trigger_message_id"], ["messages.id"]),
    )
    op.create_index(
        "ix_analysis_tasks_user_session_created",
        "analysis_tasks",
        ["user_id", "session_id", "created_at"],
    )
    op.create_index(
        "ix_analysis_tasks_status_lease",
        "analysis_tasks",
        ["status", "lease_expires_at"],
    )

    op.create_table(
        "task_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["analysis_tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_task_events_task_id_id", "task_events", ["task_id", "id"])

    op.create_table(
        "model_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(36), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("prompt_template", sa.String(64), nullable=False),
        sa.Column("prompt_version", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("cached_tokens", sa.Integer(), nullable=True),
        sa.Column("reasoning_tokens", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_type", sa.String(64), nullable=True),
        sa.Column("request_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["analysis_tasks.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_model_runs_task_created", "model_runs", ["task_id", "created_at"]
    )

    op.create_table(
        "mcp_tool_catalog",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("service_slug", sa.String(64), nullable=False),
        sa.Column("internal_tool_name", sa.String(128), nullable=False),
        sa.Column("reviewed_description", sa.Text(), nullable=False),
        sa.Column("input_schema_json", sa.JSON(), nullable=False),
        sa.Column("output_validator_version", sa.String(32), nullable=False),
        sa.Column("discovery_digest", sa.String(64), nullable=False),
        sa.Column("review_status", sa.String(32), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(SERVICE_SLUG_CHECK, name="ck_mcp_tool_catalog_service_slug"),
        sa.UniqueConstraint(
            "internal_tool_name", name="uq_mcp_tool_catalog_internal_name"
        ),
    )
    op.create_index(
        "ix_mcp_tool_catalog_service_enabled",
        "mcp_tool_catalog",
        ["service_slug", "is_enabled"],
    )

    op.create_table(
        "mcp_calls",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("logical_call_id", sa.String(36), nullable=False),
        sa.Column("task_id", sa.String(36), nullable=False),
        sa.Column("batch_no", sa.Integer(), nullable=False),
        sa.Column("plan_step_id", sa.String(64), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("service_slug", sa.String(64), nullable=False),
        sa.Column("internal_tool_name", sa.String(128), nullable=False),
        sa.Column("arguments_digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reservation_transaction_id", sa.String(36), nullable=True),
        sa.Column("settlement_transaction_id", sa.String(36), nullable=True),
        sa.Column("upstream_request_id", sa.String(128), nullable=True),
        sa.Column("protocol_session_digest", sa.String(64), nullable=True),
        sa.Column("response_hash", sa.String(64), nullable=True),
        sa.Column("evidence_json", sa.JSON(), nullable=True),
        sa.Column("error_type", sa.String(64), nullable=True),
        sa.Column("error_message", sa.String(500), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(SERVICE_SLUG_CHECK, name="ck_mcp_calls_service_slug"),
        sa.CheckConstraint(MCP_CALL_STATUS_CHECK, name="ck_mcp_calls_status"),
        sa.ForeignKeyConstraint(["task_id"], ["analysis_tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["reservation_transaction_id"], ["wallet_transactions.id"]
        ),
        sa.ForeignKeyConstraint(
            ["settlement_transaction_id"], ["wallet_transactions.id"]
        ),
        sa.UniqueConstraint("logical_call_id", name="uq_mcp_calls_logical_call_id"),
        sa.UniqueConstraint(
            "task_id", "plan_step_id", "attempt", name="uq_mcp_calls_task_step_attempt"
        ),
        sa.UniqueConstraint(
            "settlement_transaction_id", name="uq_mcp_calls_settlement_transaction"
        ),
    )
    op.create_index(
        "ix_mcp_calls_status_updated", "mcp_calls", ["status", "updated_at"]
    )


def downgrade() -> None:
    op.drop_table("mcp_calls")
    op.drop_table("mcp_tool_catalog")
    op.drop_table("model_runs")
    op.drop_table("task_events")
    op.drop_table("analysis_tasks")
