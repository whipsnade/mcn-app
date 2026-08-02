"""Add unified agent runtime (v3) tables and extend artifact_read_states.

Adds the 20 tables from the model-led agent runtime data model. Purely
additive — no legacy table is dropped or altered in a breaking way. The
`artifact_read_states` table already exists with legacy columns
(module_key/last_seen_artifact_id/seen_at); this migration only adds the new
read-cursor columns and the v2 unique constraint alongside them.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision: str = "0027_agent_runtime_v3"
down_revision: str | None = "0026_brand_report_v2_payload"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # agent_sessions 先于所有依赖它的表创建。
    op.create_table(
        "agent_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("session_summary", mysql.MEDIUMTEXT(), nullable=True),
        sa.Column("summary_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
    )
    # agent_artifacts 先于 memory_entries 创建（memory_entries.source_artifact_id 外键）。
    op.create_table(
        "agent_artifacts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(36),
            sa.ForeignKey("agent_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("module", sa.String(32), nullable=False),
        sa.Column("artifact_type", sa.String(48), nullable=False),
        sa.Column(
            "parent_artifact_id",
            sa.String(36),
            sa.ForeignKey("agent_artifacts.id"),
            nullable=True,
        ),
        sa.Column("artifact_key", sa.String(191), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("latest_version", sa.Integer(), nullable=False),
        sa.Column("activity_sequence", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("session_id", "artifact_key", name="uq_agent_artifacts_session_key"),
        sa.CheckConstraint(
            "status IN ('draft','reviewing','published','failed')",
            name="ck_agent_artifacts_status",
        ),
    )
    # agent_runs 与 agent_messages 互为外键：agent_runs.input_message_id 外键延后创建。
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(36),
            sa.ForeignKey("agent_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("input_message_id", sa.String(36), nullable=True),
        sa.Column(
            "parent_run_id",
            sa.String(36),
            sa.ForeignKey("agent_runs.id"),
            nullable=True,
        ),
        sa.Column("run_kind", sa.String(16), nullable=False),
        sa.Column("visibility", sa.String(16), nullable=False),
        sa.Column("profile_name", sa.String(64), nullable=False),
        sa.Column("profile_version", sa.String(16), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("prompt_snapshot_json", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("outcome", sa.String(24), nullable=True),
        sa.Column("decision_count", sa.Integer(), nullable=False),
        sa.Column("review_count", sa.Integer(), nullable=False),
        sa.Column("revision_count", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("paused_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("lease_owner", sa.String(64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("run_kind IN ('user','internal')", name="ck_agent_runs_kind"),
        sa.CheckConstraint("visibility IN ('user','internal')", name="ck_agent_runs_visibility"),
        sa.Index("ix_agent_runs_status_lease", "status", "lease_expires_at"),
    )
    op.create_table(
        "agent_messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(36),
            sa.ForeignKey("agent_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("agent_runs.id"),
            nullable=True,
        ),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", mysql.MEDIUMTEXT(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("session_id", "sequence", name="uq_agent_messages_session_sequence"),
    )
    op.create_table(
        "artifact_drafts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "artifact_id",
            sa.String(36),
            sa.ForeignKey("agent_artifacts.id"),
            nullable=False,
        ),
        sa.Column(
            "session_id",
            sa.String(36),
            sa.ForeignKey("agent_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "owner_run_id",
            sa.String(36),
            sa.ForeignKey("agent_runs.id"),
            nullable=True,
        ),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("review_count", sa.Integer(), nullable=False),
        sa.Column("revision_count", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("artifact_id", name="uq_artifact_drafts_artifact"),
        sa.CheckConstraint(
            "status IN ('idle','drafting','reviewing','failed')",
            name="ck_artifact_drafts_status",
        ),
    )
    # artifact_draft_revisions 与 agent_artifact_versions 互为外键：
    # parent_artifact_version_id 外键延后创建。
    op.create_table(
        "artifact_draft_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "draft_id",
            sa.String(36),
            sa.ForeignKey("artifact_drafts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "artifact_id",
            sa.String(36),
            sa.ForeignKey("agent_artifacts.id"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("agent_runs.id"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.String(32), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=True),
        sa.Column("evidence_refs_json", sa.JSON(), nullable=True),
        sa.Column("parent_artifact_version_id", sa.String(36), nullable=True),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "draft_id", "revision", name="uq_artifact_draft_revisions_draft_revision"
        ),
    )
    op.create_table(
        "artifact_review_batches",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "parent_run_id",
            sa.String(36),
            sa.ForeignKey("agent_runs.id"),
            nullable=False,
        ),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("completion_text", mysql.MEDIUMTEXT(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("parent_run_id", name="uq_artifact_review_batches_parent_run"),
    )
    op.create_table(
        "artifact_review_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "batch_id",
            sa.String(36),
            sa.ForeignKey("artifact_review_batches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "artifact_id",
            sa.String(36),
            sa.ForeignKey("agent_artifacts.id"),
            nullable=False,
        ),
        sa.Column(
            "draft_revision_id",
            sa.String(36),
            sa.ForeignKey("artifact_draft_revisions.id"),
            nullable=False,
        ),
        sa.Column("status", sa.String(24), nullable=False),
        sa.UniqueConstraint("batch_id", "artifact_id", name="uq_artifact_review_items_batch_artifact"),
    )
    op.create_table(
        "artifact_review_attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "review_item_id",
            sa.String(36),
            sa.ForeignKey("artifact_review_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column(
            "draft_revision_id",
            sa.String(36),
            sa.ForeignKey("artifact_draft_revisions.id"),
            nullable=False,
        ),
        sa.Column(
            "review_run_id",
            sa.String(36),
            sa.ForeignKey("agent_runs.id"),
            nullable=False,
        ),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("issues_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("review_item_id", "attempt", name="uq_artifact_review_attempt"),
        sa.CheckConstraint(
            "decision IN ('approve','revise','reject')",
            name="ck_artifact_review_attempts_decision",
        ),
    )
    op.create_table(
        "agent_artifact_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "artifact_id",
            sa.String(36),
            sa.ForeignKey("agent_artifacts.id"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "source_run_id",
            sa.String(36),
            sa.ForeignKey("agent_runs.id"),
            nullable=False,
        ),
        sa.Column(
            "source_draft_revision_id",
            sa.String(36),
            sa.ForeignKey("artifact_draft_revisions.id"),
            nullable=False,
        ),
        sa.Column(
            "parent_artifact_version_id",
            sa.String(36),
            sa.ForeignKey("agent_artifact_versions.id"),
            nullable=True,
        ),
        sa.Column("schema_version", sa.String(32), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=True),
        sa.Column("evidence_refs_json", sa.JSON(), nullable=True),
        sa.Column("review_json", sa.JSON(), nullable=True),
        sa.Column("data_status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "artifact_id", "version", name="uq_agent_artifact_versions_artifact_version"
        ),
    )
    op.create_foreign_key(
        "fk_artifact_draft_revisions_parent_artifact_version_id",
        "artifact_draft_revisions",
        "agent_artifact_versions",
        ["parent_artifact_version_id"],
        ["id"],
    )
    op.create_table(
        "artifact_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(36),
            sa.ForeignKey("agent_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("module", sa.String(32), nullable=False),
        sa.Column(
            "artifact_id",
            sa.String(36),
            sa.ForeignKey("agent_artifacts.id"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("draft_revision", sa.Integer(), nullable=True),
        sa.Column(
            "artifact_version_id",
            sa.String(36),
            sa.ForeignKey("agent_artifact_versions.id"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("session_id", "sequence", name="uq_artifact_events_session_sequence"),
        sa.CheckConstraint(
            "event_type IN ('draft_created','draft_updated','reviewing','published','failed')",
            name="ck_artifact_events_event_type",
        ),
    )
    op.create_table(
        "kol_detail_cache",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "session_id",
            sa.String(36),
            sa.ForeignKey("agent_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("kol_uid", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.String(32), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=True),
        sa.Column("evidence_refs_json", sa.JSON(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "user_id",
            "session_id",
            "platform",
            "kol_uid",
            name="uq_kol_detail_cache_user_session_platform_kol",
        ),
    )
    op.create_table(
        "agent_run_attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("decision_count", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(24), nullable=False),
        sa.UniqueConstraint("run_id", "attempt", name="uq_agent_run_attempts_run_attempt"),
        sa.CheckConstraint(
            "outcome IN ('running','paused','completed','failed','cancelled')",
            name="ck_agent_run_attempts_outcome",
        ),
    )
    op.create_table(
        "agent_steps",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "attempt_id",
            sa.String(36),
            sa.ForeignKey("agent_run_attempts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("step_type", sa.String(32), nullable=False),
        sa.Column("input_json", sa.JSON(), nullable=True),
        sa.Column("output_json", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("thinking_text", mysql.MEDIUMTEXT(), nullable=True),
        sa.Column("visibility", sa.String(16), nullable=False),
        sa.Column("model_request_id", sa.String(64), nullable=True),
        sa.Column("token_usage_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("run_id", "sequence", name="uq_agent_steps_run_sequence"),
        sa.CheckConstraint("visibility IN ('user','internal')", name="ck_agent_steps_visibility"),
    )
    op.create_table(
        "agent_tool_calls",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "step_id",
            sa.String(36),
            sa.ForeignKey("agent_steps.id"),
            nullable=False,
        ),
        sa.Column("logical_call_id", sa.String(64), nullable=False),
        sa.Column("service", sa.String(64), nullable=False),
        sa.Column("internal_tool_name", sa.String(128), nullable=False),
        sa.Column("arguments_json", sa.JSON(), nullable=True),
        sa.Column("arguments_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("points_reserved", sa.Integer(), nullable=False),
        sa.Column("points_settled", sa.Integer(), nullable=False),
        sa.Column("upstream_request_id", sa.String(128), nullable=True),
        sa.Column("error_type", sa.String(64), nullable=True),
        sa.Column("safe_error_message", sa.String(500), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("logical_call_id", name="uq_agent_tool_calls_logical_call_id"),
        sa.CheckConstraint(
            "status IN ('planned','reserved','running','settled','failed','unknown')",
            name="ck_agent_tool_calls_status",
        ),
    )
    op.create_table(
        "evidence_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(36),
            sa.ForeignKey("agent_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tool_call_id",
            sa.String(36),
            sa.ForeignKey("agent_tool_calls.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_name", sa.String(128), nullable=False),
        sa.Column("scope_json", sa.JSON(), nullable=True),
        sa.Column("period_json", sa.JSON(), nullable=True),
        sa.Column("raw_payload_json", sa.JSON(), nullable=True),
        sa.Column("normalized_preview_json", sa.JSON(), nullable=True),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("collected_at", sa.DateTime(), nullable=False),
        sa.Column("availability_status", sa.String(32), nullable=False),
    )
    op.create_table(
        "agent_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("run_id", "sequence", name="uq_agent_events_run_sequence"),
    )
    op.create_table(
        "agent_tool_call_reconciliations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "tool_call_id",
            sa.String(36),
            sa.ForeignKey("agent_tool_calls.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column(
            "actor_user_id",
            sa.String(36),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column("note", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "source IN ('upstream_probe','admin')",
            name="ck_agent_tool_call_reconciliations_source",
        ),
        sa.CheckConstraint(
            "decision IN ('confirm_success','confirm_failure','keep_unknown')",
            name="ck_agent_tool_call_reconciliations_decision",
        ),
    )
    op.create_table(
        "memory_entries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(36),
            sa.ForeignKey("agent_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_run_id",
            sa.String(36),
            sa.ForeignKey("agent_runs.id"),
            nullable=True,
        ),
        sa.Column(
            "source_artifact_id",
            sa.String(36),
            sa.ForeignKey("agent_artifacts.id"),
            nullable=True,
        ),
        sa.Column("memory_type", sa.String(32), nullable=False),
        sa.Column("content_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("superseded_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "memory_type IN ('run_summary','artifact_index','pending_question')",
            name="ck_memory_entries_type",
        ),
    )
    op.create_foreign_key(
        "fk_agent_runs_input_message_id_agent_messages",
        "agent_runs",
        "agent_messages",
        ["input_message_id"],
        ["id"],
    )
    # artifact_read_states 已存在（0022 旧表）：仅新增新读游标列与 v2 唯一约束。
    # 新列 nullable，避免破坏仍只写 module_key/seen_at 的旧写入方。
    op.add_column("artifact_read_states", sa.Column("module", sa.String(32), nullable=True))
    op.add_column(
        "artifact_read_states", sa.Column("last_seen_sequence", sa.Integer(), nullable=True)
    )
    op.add_column(
        "artifact_read_states", sa.Column("updated_at", sa.DateTime(), nullable=True)
    )
    op.create_unique_constraint(
        "uq_artifact_read_states_user_session_module_v2",
        "artifact_read_states",
        ["user_id", "session_id", "module"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_artifact_read_states_user_session_module_v2",
        "artifact_read_states",
        type_="unique",
    )
    op.drop_column("artifact_read_states", "updated_at")
    op.drop_column("artifact_read_states", "last_seen_sequence")
    op.drop_column("artifact_read_states", "module")

    # 先删除延后创建的外键约束再删表：agent_runs 与 artifact_draft_revisions 上的
    # 循环外键分别指向 agent_messages / agent_artifact_versions，MySQL 要求先解除
    # 引用约束，才能 drop 被引用的表。
    op.drop_constraint(
        "fk_agent_runs_input_message_id_agent_messages", "agent_runs", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_artifact_draft_revisions_parent_artifact_version_id",
        "artifact_draft_revisions",
        type_="foreignkey",
    )

    op.drop_table("memory_entries")
    op.drop_table("agent_tool_call_reconciliations")
    op.drop_table("agent_events")
    op.drop_table("evidence_items")
    op.drop_table("agent_tool_calls")
    op.drop_table("agent_steps")
    op.drop_table("agent_run_attempts")
    op.drop_table("kol_detail_cache")
    op.drop_table("artifact_events")
    op.drop_table("agent_artifact_versions")
    op.drop_table("artifact_review_attempts")
    op.drop_table("artifact_review_items")
    op.drop_table("artifact_review_batches")
    op.drop_table("artifact_draft_revisions")
    op.drop_table("artifact_drafts")
    op.drop_table("agent_messages")
    op.drop_table("agent_runs")
    op.drop_table("agent_artifacts")
    op.drop_table("agent_sessions")
