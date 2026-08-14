"""0039 authenticated Pi Gateway control-plane state."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0039_pi_gateway_control_plane"
down_revision: str | None = "0038_runtime_config_secrets"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pi_gateway_instances",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("gateway_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("mode", sa.String(16), nullable=False, server_default="active"),
        sa.Column("desired_capacity", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("gateway_id", name="uq_pi_gateway_instances_gateway_id"),
        sa.CheckConstraint("mode IN ('active','draining')", name="ck_pi_gateway_instances_mode"),
        sa.CheckConstraint("status IN ('active','offline','disabled')", name="ck_pi_gateway_instances_status"),
        sa.CheckConstraint("desired_capacity >= 0", name="ck_pi_gateway_instances_capacity"),
    )
    op.create_table(
        "pi_gateway_request_nonces",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("gateway_id", sa.String(64), nullable=False),
        sa.Column("nonce", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("gateway_id", "nonce", name="uq_pi_gateway_nonce_gateway_nonce"),
        sa.Index("ix_pi_gateway_request_nonces_expires", "expires_at"),
    )
    op.create_table(
        "pi_tenant_queue_states",
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("last_claimed_at", sa.DateTime(), nullable=True),
        sa.Column("active_runs", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tenant_id"),
        sa.Index("ix_pi_tenant_queue_states_claimed", "last_claimed_at"),
    )
    op.add_column("agent_sessions", sa.Column("active_run_id", sa.String(36), nullable=True))
    op.create_foreign_key(
        "fk_agent_sessions_active_run",
        "agent_sessions",
        "agent_runs",
        ["active_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column("agent_runs", sa.Column("gateway_id", sa.String(64), nullable=True))
    op.add_column("agent_runs", sa.Column("gateway_lease_hash", sa.String(128), nullable=True))
    op.add_column("agent_runs", sa.Column("gateway_lease_expires_at", sa.DateTime(), nullable=True))
    op.add_column(
        "agent_runs",
        sa.Column("infrastructure_retry_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.create_index(
        "ix_agent_runs_gateway_lease",
        "agent_runs",
        ["gateway_id", "gateway_lease_expires_at"],
    )
    op.add_column("agent_events", sa.Column("source_event_id", sa.String(160), nullable=True))
    op.create_unique_constraint(
        "uq_agent_events_run_source_event", "agent_events", ["run_id", "source_event_id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_agent_events_run_source_event", "agent_events", type_="unique")
    op.drop_column("agent_events", "source_event_id")
    op.drop_index("ix_agent_runs_gateway_lease", table_name="agent_runs")
    op.drop_column("agent_runs", "infrastructure_retry_count")
    op.drop_column("agent_runs", "gateway_lease_expires_at")
    op.drop_column("agent_runs", "gateway_lease_hash")
    op.drop_column("agent_runs", "gateway_id")
    op.drop_constraint("fk_agent_sessions_active_run", "agent_sessions", type_="foreignkey")
    op.drop_column("agent_sessions", "active_run_id")
    op.drop_table("pi_tenant_queue_states")
    op.drop_table("pi_gateway_request_nonces")
    op.drop_table("pi_gateway_instances")
