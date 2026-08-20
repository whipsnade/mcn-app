"""0047 make global Skill scopes unique under MySQL NULL semantics."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0047_marketing_skill_scope_uniqueness"
down_revision: str | None = "0046_workbook_export_cache_key"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_GLOBAL_SCOPE_KEY = "__global__"


def _scope_default() -> sa.TextClause:
    return sa.text(f"'{_GLOBAL_SCOPE_KEY}'")


def upgrade() -> None:
    op.add_column(
        "skill_revisions",
        sa.Column("scope_key", sa.String(36), nullable=False, server_default=_scope_default()),
    )
    op.execute(
        sa.text(
            "UPDATE skill_revisions SET scope_key = tenant_id "
            "WHERE tenant_id IS NOT NULL"
        )
    )
    op.alter_column(
        "skill_revisions",
        "scope_key",
        existing_type=sa.String(36),
        server_default=None,
        existing_nullable=False,
    )
    op.drop_constraint(
        "uq_skill_revisions_tenant_name_revision",
        "skill_revisions",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_skill_revisions_tenant_name_revision",
        "skill_revisions",
        ["scope_key", "skill_name", "revision"],
    )

    op.add_column(
        "skill_activations",
        sa.Column("scope_key", sa.String(36), nullable=False, server_default=_scope_default()),
    )
    op.execute(
        sa.text(
            "UPDATE skill_activations SET scope_key = tenant_id "
            "WHERE tenant_id IS NOT NULL"
        )
    )
    op.alter_column(
        "skill_activations",
        "scope_key",
        existing_type=sa.String(36),
        server_default=None,
        existing_nullable=False,
    )
    op.drop_constraint(
        "uq_skill_activations_environment_tenant_name",
        "skill_activations",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_skill_activations_environment_tenant_name",
        "skill_activations",
        ["environment", "scope_key", "skill_name"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_skill_activations_environment_tenant_name",
        "skill_activations",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_skill_activations_environment_tenant_name",
        "skill_activations",
        ["environment", "tenant_id", "skill_name"],
    )
    op.drop_column("skill_activations", "scope_key")

    op.drop_constraint(
        "uq_skill_revisions_tenant_name_revision",
        "skill_revisions",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_skill_revisions_tenant_name_revision",
        "skill_revisions",
        ["tenant_id", "skill_name", "revision"],
    )
    op.drop_column("skill_revisions", "scope_key")
