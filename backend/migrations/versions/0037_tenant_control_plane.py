"""0037 tenant control plane: tenants, memberships and append-only licenses."""

from collections.abc import Sequence
from datetime import UTC, datetime
import json
from uuid import NAMESPACE_URL, uuid5

import sqlalchemy as sa
from alembic import op

revision: str = "0037_tenant_control_plane"
down_revision: str | None = "0036_export_claim_token"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


_FEATURES = {
    "kol_selection": True,
    "brand_analysis": True,
    "campaign_analysis": True,
    "kol_detail": True,
    "utility": True,
}


def _legacy_id(kind: str, user_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"kol-insight:{kind}:{user_id}"))


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("slug", sa.String(80), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("is_internal", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("runtime_backend", sa.String(16), nullable=False, server_default="current"),
        sa.Column("license_status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("active_license_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("slug", name="uq_tenants_slug"),
        sa.CheckConstraint("status IN ('active','disabled')", name="ck_tenants_status"),
        sa.CheckConstraint(
            "runtime_backend IN ('current','pi')", name="ck_tenants_runtime_backend"
        ),
        sa.CheckConstraint(
            "license_status IN ('active','suspended')", name="ck_tenants_license_status"
        ),
        sa.Index("ix_tenants_status_runtime", "status", "runtime_backend"),
    )
    op.create_table(
        "tenant_memberships",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("role", sa.String(16), nullable=False, server_default="member"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "user_id", name="uq_tenant_memberships_tenant_user"),
        sa.UniqueConstraint("user_id", name="uq_tenant_memberships_user"),
        sa.CheckConstraint(
            "role IN ('owner','admin','member')", name="ck_tenant_memberships_role"
        ),
        sa.CheckConstraint(
            "status IN ('active','disabled')", name="ck_tenant_memberships_status"
        ),
        sa.Index("ix_tenant_memberships_tenant_status", "tenant_id", "status"),
    )
    op.create_table(
        "tenant_licenses",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("valid_from", sa.DateTime(), nullable=False),
        sa.Column("valid_until", sa.DateTime(), nullable=True),
        sa.Column("features_json", sa.JSON(), nullable=False),
        sa.Column("max_concurrent_runs", sa.Integer(), nullable=False),
        sa.Column("max_user_concurrent_runs", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "version", name="uq_tenant_licenses_tenant_version"),
        sa.CheckConstraint("version > 0", name="ck_tenant_licenses_version_positive"),
        sa.CheckConstraint(
            "max_concurrent_runs > 0", name="ck_tenant_licenses_max_concurrent"
        ),
        sa.CheckConstraint(
            "max_user_concurrent_runs > 0", name="ck_tenant_licenses_max_user_concurrent"
        ),
        sa.Index("ix_tenant_licenses_tenant_dates", "tenant_id", "valid_from", "valid_until"),
    )
    op.create_foreign_key(
        "fk_tenants_active_license",
        "tenants",
        "tenant_licenses",
        ["active_license_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column("agent_sessions", sa.Column("tenant_id", sa.String(36), nullable=True))
    op.add_column("agent_runs", sa.Column("tenant_id", sa.String(36), nullable=True))
    op.create_foreign_key(
        "fk_agent_sessions_tenant",
        "agent_sessions",
        "tenants",
        ["tenant_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_agent_runs_tenant",
        "agent_runs",
        "tenants",
        ["tenant_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_agent_sessions_tenant_status", "agent_sessions", ["tenant_id", "status"]
    )
    op.create_index(
        "ix_agent_runs_tenant_status_created",
        "agent_runs",
        ["tenant_id", "status", "created_at", "id"],
    )

    connection = op.get_bind()
    users = connection.execute(sa.text("SELECT id, nickname, created_at, updated_at FROM users")).mappings()
    for user in users:
        user_id = str(user["id"])
        tenant_id = _legacy_id("tenant", user_id)
        license_id = _legacy_id("license", user_id)
        created_at = user["created_at"] or datetime.now(UTC).replace(tzinfo=None)
        updated_at = user["updated_at"] or created_at
        connection.execute(
            sa.text(
                "INSERT INTO tenants "
                "(id, slug, name, status, is_internal, runtime_backend, license_status, "
                "active_license_id, created_at, updated_at) "
                "VALUES (:id, :slug, :name, 'active', 0, 'current', 'active', NULL, :created_at, :updated_at)"
            ),
            {
                "id": tenant_id,
                "slug": f"legacy-{user_id[:24]}",
                "name": f"个人租户-{user['nickname'] or user_id[:8]}",
                "created_at": created_at,
                "updated_at": updated_at,
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO tenant_licenses "
                "(id, tenant_id, version, valid_from, valid_until, features_json, "
                "max_concurrent_runs, max_user_concurrent_runs, created_by, created_at) "
                "VALUES (:id, :tenant_id, 1, :valid_from, NULL, :features_json, 4, 2, :created_by, :created_at)"
            ),
            {
                "id": license_id,
                "tenant_id": tenant_id,
                "valid_from": created_at,
                "features_json": json.dumps(_FEATURES, separators=(",", ":")),
                "created_by": user_id,
                "created_at": created_at,
            },
        )
        connection.execute(
            sa.text("UPDATE tenants SET active_license_id = :license_id WHERE id = :tenant_id"),
            {"license_id": license_id, "tenant_id": tenant_id},
        )
        connection.execute(
            sa.text(
                "INSERT INTO tenant_memberships "
                "(id, tenant_id, user_id, role, status, created_at, updated_at) "
                "VALUES (:id, :tenant_id, :user_id, 'owner', 'active', :created_at, :updated_at)"
            ),
            {
                "id": _legacy_id("membership", user_id),
                "tenant_id": tenant_id,
                "user_id": user_id,
                "created_at": created_at,
                "updated_at": updated_at,
            },
        )
    connection.execute(
        sa.text(
            "UPDATE agent_sessions s JOIN tenant_memberships m ON m.user_id = s.user_id "
            "SET s.tenant_id = m.tenant_id WHERE s.tenant_id IS NULL"
        )
    )
    mismatch_count = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM agent_runs r "
            "JOIN agent_sessions s ON s.id = r.session_id "
            "WHERE s.tenant_id IS NULL OR r.user_id <> s.user_id"
        )
    ).scalar_one()
    if mismatch_count:
        raise RuntimeError("0037_run_session_tenant_mismatch")
    connection.execute(
        sa.text(
            "UPDATE agent_runs r JOIN agent_sessions s ON s.id = r.session_id "
            "SET r.tenant_id = s.tenant_id WHERE r.tenant_id IS NULL"
        )
    )
    op.alter_column("agent_sessions", "tenant_id", existing_type=sa.String(36), nullable=False)
    op.alter_column("agent_runs", "tenant_id", existing_type=sa.String(36), nullable=False)


def downgrade() -> None:
    connection = op.get_bind()
    non_legacy_tenants = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM tenants WHERE slug NOT LIKE 'legacy-%'"
        )
    ).scalar_one()
    if non_legacy_tenants:
        raise RuntimeError("0037_downgrade_refused_non_legacy_tenant")
    op.drop_constraint("fk_agent_runs_tenant", "agent_runs", type_="foreignkey")
    op.drop_constraint("fk_agent_sessions_tenant", "agent_sessions", type_="foreignkey")
    op.drop_index("ix_agent_runs_tenant_status_created", table_name="agent_runs")
    op.drop_index("ix_agent_sessions_tenant_status", table_name="agent_sessions")
    op.drop_column("agent_runs", "tenant_id")
    op.drop_column("agent_sessions", "tenant_id")
    op.drop_constraint("fk_tenants_active_license", "tenants", type_="foreignkey")
    op.drop_table("tenant_licenses")
    op.drop_table("tenant_memberships")
    op.drop_table("tenants")
