from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
# Load agent_artifacts tables so agent_runtime.models imports standalone without
# relying on db/models.py to register the FK target (memory_entries.source_artifact_id).
import app.agent_artifacts.models  # noqa: F401


def _utc_now() -> datetime:
    # 与 repository.utc_now 同义；models 不能反向 import repository（循环依赖）。
    return datetime.now(UTC).replace(tzinfo=None)


class AgentSession(Base):
    __tablename__ = "agent_sessions"
    __table_args__ = (Index("ix_agent_sessions_tenant_status", "tenant_id", "status"),)

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # 0037 backfills historical sessions; all new production creators must set it.
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    active_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    session_summary: Mapped[str | None] = mapped_column(MEDIUMTEXT, nullable=True)
    summary_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AgentMessage(Base):
    __tablename__ = "agent_messages"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence", name="uq_agent_messages_session_sequence"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_runs.id"), nullable=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(MEDIUMTEXT, nullable=False)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint("run_kind IN ('user','internal')", name="ck_agent_runs_kind"),
        CheckConstraint("visibility IN ('user','internal')", name="ck_agent_runs_visibility"),
        Index("ix_agent_runs_status_lease", "status", "lease_expires_at"),
        Index(
            "ix_agent_runs_tenant_backend_status_queue",
            "tenant_id",
            "runtime_backend",
            "status",
            "queued_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # 0037 backfills historical runs; all new production creators must set it.
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    runtime_backend: Mapped[str] = mapped_column(String(16), nullable=False, default="current")
    runtime_config_version_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("runtime_config_versions.id", ondelete="RESTRICT"), nullable=False
    )
    runtime_config_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    queued_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_now)
    input_message_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_messages.id", use_alter=True), nullable=True
    )
    parent_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_runs.id"), nullable=True
    )
    run_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    visibility: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    profile_name: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_version: Mapped[str] = mapped_column(String(16), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_snapshot_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued")
    outcome: Mapped[str | None] = mapped_column(String(24), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    decision_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    review_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    revision_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    gateway_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    gateway_lease_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    gateway_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    infrastructure_retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    # 会话详情 runs 的稳定排序键（迁移 0029）：按创建时刻升序、id tie-break，
    # 前端取列表最后一个即最新 Run，不再受随机 uuid 顺序影响。
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utc_now
    )


class AgentRunAttempt(Base):
    __tablename__ = "agent_run_attempts"
    __table_args__ = (
        UniqueConstraint("run_id", "attempt", name="uq_agent_run_attempts_run_attempt"),
        CheckConstraint(
            "outcome IN ('running','paused','completed','failed','cancelled')",
            name="ck_agent_run_attempts_outcome",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decision_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    outcome: Mapped[str] = mapped_column(String(24), nullable=False, default="running")


class AgentStep(Base):
    __tablename__ = "agent_steps"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_agent_steps_run_sequence"),
        CheckConstraint("visibility IN ('user','internal')", name="ck_agent_steps_visibility"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    attempt_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_run_attempts.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    step_type: Mapped[str] = mapped_column(String(32), nullable=False)
    input_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    output_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    thinking_text: Mapped[str | None] = mapped_column(MEDIUMTEXT, nullable=True)
    visibility: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    model_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    token_usage_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class AgentToolCall(Base):
    __tablename__ = "agent_tool_calls"
    __table_args__ = (
        UniqueConstraint("logical_call_id", name="uq_agent_tool_calls_logical_call_id"),
        CheckConstraint(
            "status IN ('planned','reserved','running','settled','failed','unknown')",
            name="ck_agent_tool_calls_status",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    step_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_steps.id"), nullable=False
    )
    logical_call_id: Mapped[str] = mapped_column(String(64), nullable=False)
    service: Mapped[str] = mapped_column(String(64), nullable=False)
    internal_tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    arguments_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    dispatch_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="planned")
    points_reserved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    points_settled: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    upstream_request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    safe_error_message: Mapped[str | None] = mapped_column(MEDIUMTEXT, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AgentUpload(Base):
    """用户上传文件的可审计元数据；文件本体按 storage_key 存本地目录。

    原始上传不可变：重新解析产生新 Evidence，不覆盖来源。status 白名单
    uploaded/parsed/failed 由 ck_agent_uploads_status 强制。
    """

    __tablename__ = "agent_uploads"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_sessions.id"), nullable=False
    )
    run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_runs.id"), nullable=True
    )
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class EvidenceItem(Base):
    """Immutable evidence captured from read-only tools; models only obtain it via tools."""

    __tablename__ = "evidence_items"
    __table_args__ = (
        CheckConstraint(
            "((tool_call_id IS NULL) <> (upload_id IS NULL))",
            name="ck_evidence_items_tool_call_xor_upload",
        ),
        Index(
            "ix_evidence_items_session_collected_at",
            "session_id",
            "collected_at",
        ),
        Index("ix_evidence_items_upload_id", "upload_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False
    )
    # 可空：upload Evidence 在 Run 创建前落库（迁移 0032）；MCP Evidence 必带。
    run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=True
    )
    tool_call_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_tool_calls.id", ondelete="CASCADE"), nullable=True
    )
    upload_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_uploads.id", ondelete="CASCADE"), nullable=True
    )
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_name: Mapped[str] = mapped_column(String(128), nullable=False)
    scope_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    period_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    raw_payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    normalized_preview_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    collected_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    availability_status: Mapped[str] = mapped_column(String(32), nullable=False, default="available")
    normalization_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    normalization_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    field_mapping_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    unmapped_fields_json: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    truncated: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    normalization_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AgentEvent(Base):
    __tablename__ = "agent_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_agent_events_run_sequence"),
        UniqueConstraint("run_id", "source_event_id", name="uq_agent_events_run_source_event"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    source_event_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class AgentToolCallReconciliation(Base):
    """Immutable audit trail for reconciling tool calls with unknown results; append-only."""

    __tablename__ = "agent_tool_call_reconciliations"
    __table_args__ = (
        CheckConstraint(
            "source IN ('upstream_probe','admin')",
            name="ck_agent_tool_call_reconciliations_source",
        ),
        CheckConstraint(
            "decision IN ('confirm_success','confirm_failure','keep_unknown')",
            name="ck_agent_tool_call_reconciliations_decision",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    tool_call_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_tool_calls.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True
    )
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class MemoryEntry(Base):
    __tablename__ = "memory_entries"
    __table_args__ = (
        CheckConstraint(
            "memory_type IN ('run_summary','artifact_index','pending_question','confirmed_scope')",
            name="ck_memory_entries_type",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False
    )
    source_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_runs.id"), nullable=True
    )
    source_artifact_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_artifacts.id"), nullable=True
    )
    memory_type: Mapped[str] = mapped_column(String(32), nullable=False)
    content_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
