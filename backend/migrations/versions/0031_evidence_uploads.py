"""上传证据与归一化诊断：agent_uploads + evidence_items.upload_id/XOR + 诊断列。

用户 CSV/XLSX 上传先落 ``agent_uploads``（不可变文件哈希 + 本地存储键，
状态 uploaded/parsed/failed），解析结果作为 upload Evidence 写入
``evidence_items``。``evidence_items`` 增加 nullable ``upload_id`` FK，
与 ``tool_call_id`` 构成 XOR Check Constraint（Evidence 必须且只能关联
工具调用或上传之一）；新增归一化诊断列（version/status/field_mapping/
unmapped_fields/truncated/error_code），DataTap 成功 payload 的字段映射
诊断随 Evidence 持久化。downgrade 只移除本迁移创建的对象。
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0031_evidence_uploads"
down_revision: str | None = "0030_direct_publish_runtime"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # 不显式建 user_id/session_id 索引：MySQL 自动为 FK 创建单列索引，
    # downgrade 的 drop_table 会连带删除 FK 与索引（显式索引会被 FK 复用，
    # 单独 DROP 报 MySQL 1553）。
    op.create_table(
        "agent_uploads",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "session_id",
            sa.String(36),
            sa.ForeignKey("agent_sessions.id"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("agent_runs.id"),
            nullable=True,
        ),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("mime_type", sa.String(128), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.String(255), nullable=False),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
        ),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('uploaded','parsed','failed')",
            name="ck_agent_uploads_status",
        ),
    )

    # evidence_items：tool_call_id 可空 + upload_id XOR（Evidence 必须且只能
    # 关联 tool_call_id 或 upload_id 之一）。
    op.alter_column(
        "evidence_items",
        "tool_call_id",
        existing_type=sa.String(36),
        nullable=True,
    )
    # 先建列与索引再补 FK：MySQL 建 FK 时复用已有以 upload_id 开头的索引，
    # 避免自动索引与显式索引重复。
    op.add_column(
        "evidence_items",
        sa.Column("upload_id", sa.String(36), nullable=True),
    )
    op.create_index("ix_evidence_items_upload_id", "evidence_items", ["upload_id"])
    op.create_foreign_key(
        "fk_evidence_items_upload_id_agent_uploads",
        "evidence_items",
        "agent_uploads",
        ["upload_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_check_constraint(
        "ck_evidence_items_tool_call_xor_upload",
        "evidence_items",
        "((tool_call_id IS NULL) <> (upload_id IS NULL))",
    )

    # 归一化诊断列：DataTap 成功 payload 的字段映射与未映射字段随 Evidence 落库。
    op.add_column(
        "evidence_items",
        sa.Column("normalization_version", sa.String(32), nullable=True),
    )
    op.add_column(
        "evidence_items",
        sa.Column("normalization_status", sa.String(32), nullable=True),
    )
    op.add_column(
        "evidence_items",
        sa.Column("field_mapping_json", sa.JSON(), nullable=True),
    )
    op.add_column(
        "evidence_items",
        sa.Column("unmapped_fields_json", sa.JSON(), nullable=True),
    )
    op.add_column(
        "evidence_items",
        sa.Column("truncated", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "evidence_items",
        sa.Column("normalization_error_code", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_evidence_items_session_collected_at",
        "evidence_items",
        ["session_id", "collected_at"],
    )


def _drop_session_foreign_key() -> None:
    """动态获取 session_id 的 FK 约束名并删除。

    MySQL 8 会把 ``ix_evidence_items_session_collected_at`` 用作 session_id
    FK 的支撑索引（表上没有单列 session_id 索引），直接 DROP INDEX 报 1553。
    先删 FK 再删索引、最后重建 FK（MySQL 自动补单列索引）。
    """
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT CONSTRAINT_NAME FROM information_schema.KEY_COLUMN_USAGE "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'evidence_items' "
            "AND COLUMN_NAME = 'session_id' AND REFERENCED_TABLE_NAME = 'agent_sessions'"
        )
    ).fetchall()
    for (constraint_name,) in rows:
        op.drop_constraint(constraint_name, "evidence_items", type_="foreignkey")


def downgrade() -> None:
    op.drop_column("evidence_items", "normalization_error_code")
    op.drop_column("evidence_items", "truncated")
    op.drop_column("evidence_items", "unmapped_fields_json")
    op.drop_column("evidence_items", "field_mapping_json")
    op.drop_column("evidence_items", "normalization_status")
    op.drop_column("evidence_items", "normalization_version")
    op.drop_constraint(
        "ck_evidence_items_tool_call_xor_upload",
        "evidence_items",
        type_="check",
    )
    # 先删 FK 再删索引与列：ix_evidence_items_upload_id 支撑 FK，列上残留
    # 外键时 MySQL 拒绝 drop column。
    op.drop_constraint(
        "fk_evidence_items_upload_id_agent_uploads",
        "evidence_items",
        type_="foreignkey",
    )
    op.drop_index("ix_evidence_items_upload_id", table_name="evidence_items")
    op.drop_column("evidence_items", "upload_id")
    op.alter_column(
        "evidence_items",
        "tool_call_id",
        existing_type=sa.String(36),
        nullable=False,
    )
    # 复合索引被 session FK 支撑：删 FK → 删索引 → 重建 FK（顺序见
    # _drop_session_foreign_key 说明）。
    _drop_session_foreign_key()
    op.drop_index(
        "ix_evidence_items_session_collected_at", table_name="evidence_items"
    )
    op.create_foreign_key(
        "fk_evidence_items_session_id_agent_sessions",
        "evidence_items",
        "agent_sessions",
        ["session_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # agent_uploads 的 FK 与索引均由 MySQL 自动管理，drop_table 连带删除。
    op.drop_table("agent_uploads")
