"""0045 create immutable marketing Skill revisions and activation pointers.

The migration stores an audited marketing-v2 baseline in ``skill_revisions`` and
points production at those immutable rows through ``skill_activations``.  No
runtime code reads the mutable source tree after this migration.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision: str = "0045_marketing_skill_registry"
down_revision: str | None = "0044_agent_run_loop_guard"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


_BASELINE_SKILLS = (
    (
        "social-marketing-analyst",
        "社交营销分析领域知识",
        None,
        "根据用户问题自主选择已审核工具，披露数据范围与限制。",
    ),
    (
        "brand-research-report",
        "品牌研究报告输出指导",
        "brand_report_v3",
        "按真实数据生成品牌研究报告，缺失字段必须披露。",
    ),
    (
        "campaign-evaluation-report",
        "活动评估报告输出指导",
        "campaign_report_v3",
        "按真实数据生成活动评估报告，不编造数量或指标。",
    ),
    (
        "kol-selection-report",
        "达人筛选报告输出指导",
        "kol_selection_v3",
        "按用户目标和真实证据生成达人筛选结果，不使用固定业务行数门禁。",
    ),
    (
        "marketing-strategy",
        "营销策略分析领域知识",
        "strategy_advice_v1",
        "围绕业务目标组织分析，允许模型决定澄清、工具顺序和停止条件。",
    ),
    (
        "artifact-drilldown",
        "已发布产物钻取指导",
        "insight_board_v1",
        "只读已发布版本和证据，遵守租户归属、不可变版本和数据受限披露。",
    ),
    (
        "analysis-report",
        "通用分析报告输出指导",
        "analysis_report_v1",
        "使用 typed blocks 表达混合营销分析，真实数量不足时保留实际数量。",
    ),
    (
        "workbook-export",
        "同版 Workbook 输出指导",
        "workbook_v1",
        "只从同一不可变报告版本生成安全、确定性的 Excel 投影。",
    ),
)


def _content(name: str, description: str, body: str) -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "required_tools: []\n"
        "---\n\n"
        f"{body}\n"
    )


def _baseline_rows() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    revisions: list[dict[str, object]] = []
    activations: list[dict[str, object]] = []
    now = datetime.now(UTC).replace(tzinfo=None)
    for index, (name, description, artifact_contract, body) in enumerate(_BASELINE_SKILLS, start=1):
        content = _content(name, description, body)
        revision_id = f"00000000-0045-4000-8000-{index:012d}"
        revisions.append(
            {
                "id": revision_id,
                "tenant_id": None,
                "skill_name": name,
                "revision": 1,
                "content": content,
                "content_digest": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "description": description,
                "required_tools": [],
                "artifact_contract": artifact_contract,
                "created_by": None,
                "created_at": now,
                "change_note": "marketing-v2 baseline snapshot",
            }
        )
        activations.append(
            {
                "id": f"00000000-0045-4000-9000-{index:012d}",
                "environment": "production",
                "tenant_id": None,
                "skill_name": name,
                "active_revision_id": revision_id,
                "previous_revision_id": None,
                "rollout_percent": 100,
                "updated_by": None,
                "updated_at": now,
            }
        )
    return revisions, activations


def upgrade() -> None:
    op.create_table(
        "skill_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("skill_name", sa.String(96), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("content", mysql.MEDIUMTEXT(), nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("description", sa.String(512), nullable=False),
        sa.Column("required_tools", sa.JSON(), nullable=False),
        sa.Column("artifact_contract", sa.String(96), nullable=True),
        sa.Column(
            "created_by",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("change_note", sa.String(512), nullable=True),
        sa.UniqueConstraint(
            "tenant_id",
            "skill_name",
            "revision",
            name="uq_skill_revisions_tenant_name_revision",
        ),
        sa.CheckConstraint("revision > 0", name="ck_skill_revisions_revision_positive"),
        sa.Index("ix_skill_revisions_name_created", "skill_name", "created_at"),
        sa.Index("ix_skill_revisions_tenant_name", "tenant_id", "skill_name"),
    )
    op.create_table(
        "skill_activations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("environment", sa.String(24), nullable=False),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("skill_name", sa.String(96), nullable=False),
        sa.Column(
            "active_revision_id",
            sa.String(36),
            sa.ForeignKey("skill_revisions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "previous_revision_id",
            sa.String(36),
            sa.ForeignKey("skill_revisions.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("rollout_percent", sa.Integer(), nullable=False, server_default="100"),
        sa.Column(
            "updated_by",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "environment",
            "tenant_id",
            "skill_name",
            name="uq_skill_activations_environment_tenant_name",
        ),
        sa.CheckConstraint(
            "rollout_percent >= 0 AND rollout_percent <= 100",
            name="ck_skill_activations_rollout_percent",
        ),
        sa.Index("ix_skill_activations_environment_name", "environment", "skill_name"),
        sa.Index("ix_skill_activations_tenant_name", "tenant_id", "skill_name"),
    )
    revision_rows, activation_rows = _baseline_rows()
    revision_table = sa.table(
        "skill_revisions",
        sa.column("id", sa.String),
        sa.column("tenant_id", sa.String),
        sa.column("skill_name", sa.String),
        sa.column("revision", sa.Integer),
        sa.column("content", sa.Text),
        sa.column("content_digest", sa.String),
        sa.column("description", sa.String),
        sa.column("required_tools", sa.JSON),
        sa.column("artifact_contract", sa.String),
        sa.column("created_by", sa.String),
        sa.column("created_at", sa.DateTime),
        sa.column("change_note", sa.String),
    )
    activation_table = sa.table(
        "skill_activations",
        sa.column("id", sa.String),
        sa.column("environment", sa.String),
        sa.column("tenant_id", sa.String),
        sa.column("skill_name", sa.String),
        sa.column("active_revision_id", sa.String),
        sa.column("previous_revision_id", sa.String),
        sa.column("rollout_percent", sa.Integer),
        sa.column("updated_by", sa.String),
        sa.column("updated_at", sa.DateTime),
    )
    op.bulk_insert(revision_table, revision_rows)
    op.bulk_insert(activation_table, activation_rows)


def downgrade() -> None:
    op.drop_table("skill_activations")
    op.drop_table("skill_revisions")
