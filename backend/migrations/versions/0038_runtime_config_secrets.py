"""0038 versioned runtime configuration and encrypted secret envelopes."""

from collections.abc import Sequence
from datetime import UTC, datetime
import json

import sqlalchemy as sa
from alembic import op

revision: str = "0038_runtime_config_secrets"
down_revision: str | None = "0037_tenant_control_plane"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_LEGACY_ID = "legacy-env-v1"
_POC_ID = "poc-isolated-v1"
_CONTRACT = "marketing_runtime_v1"


def _config_payload(config_id: str, backend: str) -> str:
    return json.dumps(
        {
            "config_version_id": config_id,
            "runtime_contract_version": _CONTRACT,
            "runtime_backend": backend,
            "model": {"name": "legacy-env", "masked_origin": "environment"},
            "datatap": {"service": "environment", "schema_digest": "environment"},
            "capability_pack": {"runtime_contract_version": _CONTRACT},
            "limits": {"max_decisions": 50},
            "billing": {"mcp_call_points": 10},
        },
        separators=(",", ":"),
    )


def upgrade() -> None:
    op.create_table(
        "runtime_config_versions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("tenant_id", sa.String(36), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("runtime_backend", sa.String(16), nullable=False),
        sa.Column("runtime_contract_version", sa.String(64), nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("secret_refs_json", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("activated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "scope", "tenant_id", "version", name="uq_runtime_config_scope_tenant_version"
        ),
        sa.CheckConstraint("scope IN ('system','tenant')", name="ck_runtime_config_scope"),
        sa.CheckConstraint("status IN ('draft','active','retired')", name="ck_runtime_config_status"),
        sa.CheckConstraint(
            "runtime_backend IN ('current','pi')", name="ck_runtime_config_backend"
        ),
        sa.Index("ix_runtime_config_scope_status", "scope", "status", "tenant_id"),
    )
    op.create_table(
        "encrypted_runtime_secrets",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("secret_kind", sa.String(64), nullable=False),
        sa.Column("algorithm", sa.String(32), nullable=False, server_default="AES-256-GCM"),
        sa.Column("nonce", sa.String(64), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("key_version", sa.String(32), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("masked_value", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "secret_kind", "id", name="uq_runtime_secret_identity"),
        sa.CheckConstraint("status IN ('active','retired')", name="ck_runtime_secret_status"),
        sa.Index("ix_runtime_secret_tenant_kind", "tenant_id", "secret_kind"),
    )
    op.add_column("tenants", sa.Column("active_runtime_config_id", sa.String(64), nullable=True))
    op.create_foreign_key(
        "fk_tenants_active_runtime_config",
        "tenants",
        "runtime_config_versions",
        ["active_runtime_config_id"],
        ["id"],
        ondelete="SET NULL",
    )

    now = datetime.now(UTC).replace(tzinfo=None)
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "INSERT INTO runtime_config_versions "
            "(id, scope, tenant_id, version, status, runtime_backend, runtime_contract_version, "
            "config_json, secret_refs_json, created_by, created_at, activated_at) "
            "VALUES (:id, 'system', NULL, 1, 'active', 'current', :contract, :config, :refs, NULL, :now, :now)"
        ),
        {
            "id": _LEGACY_ID,
            "contract": _CONTRACT,
            "config": _config_payload(_LEGACY_ID, "current"),
            "refs": "[]",
            "now": now,
        },
    )
    connection.execute(
        sa.text(
            "INSERT INTO runtime_config_versions "
            "(id, scope, tenant_id, version, status, runtime_backend, runtime_contract_version, "
            "config_json, secret_refs_json, created_by, created_at, activated_at) "
            "VALUES (:id, 'system', NULL, 2, 'retired', 'pi', :contract, :config, :refs, NULL, :now, NULL)"
        ),
        {
            "id": _POC_ID,
            "contract": _CONTRACT,
            "config": _config_payload(_POC_ID, "pi"),
            "refs": "[]",
            "now": now,
        },
    )

    op.add_column("agent_runs", sa.Column("runtime_backend", sa.String(16), nullable=True))
    op.add_column("agent_runs", sa.Column("runtime_config_version_id", sa.String(64), nullable=True))
    op.add_column("agent_runs", sa.Column("runtime_config_snapshot_json", sa.JSON(), nullable=True))
    op.add_column("agent_runs", sa.Column("queued_at", sa.DateTime(), nullable=True))
    op.create_foreign_key(
        "fk_agent_runs_runtime_config",
        "agent_runs",
        "runtime_config_versions",
        ["runtime_config_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    snapshot = _config_payload(_LEGACY_ID, "current")
    connection.execute(
        sa.text(
            "UPDATE agent_runs SET runtime_backend='current', runtime_config_version_id=:config_id, "
            "runtime_config_snapshot_json=:snapshot, queued_at=created_at"
        ),
        {"config_id": _LEGACY_ID, "snapshot": snapshot},
    )
    op.alter_column("agent_runs", "runtime_backend", existing_type=sa.String(16), nullable=False)
    op.alter_column(
        "agent_runs", "runtime_config_version_id", existing_type=sa.String(64), nullable=False
    )
    op.alter_column(
        "agent_runs", "runtime_config_snapshot_json", existing_type=sa.JSON(), nullable=False
    )
    op.alter_column("agent_runs", "queued_at", existing_type=sa.DateTime(), nullable=False)
    op.create_index(
        "ix_agent_runs_tenant_backend_status_queue",
        "agent_runs",
        ["tenant_id", "runtime_backend", "status", "queued_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_runs_tenant_backend_status_queue", table_name="agent_runs")
    op.drop_constraint("fk_agent_runs_runtime_config", "agent_runs", type_="foreignkey")
    op.drop_column("agent_runs", "queued_at")
    op.drop_column("agent_runs", "runtime_config_snapshot_json")
    op.drop_column("agent_runs", "runtime_config_version_id")
    op.drop_column("agent_runs", "runtime_backend")
    op.drop_constraint("fk_tenants_active_runtime_config", "tenants", type_="foreignkey")
    op.drop_column("tenants", "active_runtime_config_id")
    op.drop_table("encrypted_runtime_secrets")
    op.drop_table("runtime_config_versions")
