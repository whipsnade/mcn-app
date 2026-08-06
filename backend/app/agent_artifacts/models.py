from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AgentArtifact(Base):
    __tablename__ = "agent_artifacts"
    __table_args__ = (
        UniqueConstraint("session_id", "artifact_key", name="uq_agent_artifacts_session_key"),
        CheckConstraint(
            "status IN ('draft','reviewing','published','failed')",
            name="ck_agent_artifacts_status",
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
    module: Mapped[str] = mapped_column(String(32), nullable=False)
    artifact_type: Mapped[str] = mapped_column(String(48), nullable=False)
    parent_artifact_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_artifacts.id"), nullable=True
    )
    artifact_key: Mapped[str] = mapped_column(String(191), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="draft")
    latest_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    activity_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ArtifactDraft(Base):
    __tablename__ = "artifact_drafts"
    __table_args__ = (
        UniqueConstraint("artifact_id", name="uq_artifact_drafts_artifact"),
        CheckConstraint(
            "status IN ('idle','drafting','reviewing','failed')",
            name="ck_artifact_drafts_status",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    artifact_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_artifacts.id"), nullable=False
    )
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False
    )
    owner_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_runs.id"), nullable=True
    )
    current_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="idle")
    review_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    revision_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ArtifactDraftRevision(Base):
    """Immutable revision; every draft update appends one before advancing current_revision."""

    __tablename__ = "artifact_draft_revisions"
    __table_args__ = (
        UniqueConstraint("draft_id", "revision", name="uq_artifact_draft_revisions_draft_revision"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    draft_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("artifact_drafts.id", ondelete="CASCADE"), nullable=False
    )
    artifact_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_artifacts.id"), nullable=False
    )
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_runs.id"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    evidence_refs_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    parent_artifact_version_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_artifact_versions.id", use_alter=True), nullable=True
    )
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ArtifactReviewBatch(Base):
    __tablename__ = "artifact_review_batches"
    __table_args__ = (
        UniqueConstraint("parent_run_id", name="uq_artifact_review_batches_parent_run"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    parent_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_runs.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    completion_text: Mapped[str | None] = mapped_column(MEDIUMTEXT, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ArtifactReviewItem(Base):
    __tablename__ = "artifact_review_items"
    __table_args__ = (
        UniqueConstraint("batch_id", "artifact_id", name="uq_artifact_review_items_batch_artifact"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    batch_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("artifact_review_batches.id", ondelete="CASCADE"), nullable=False
    )
    artifact_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_artifacts.id"), nullable=False
    )
    draft_revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("artifact_draft_revisions.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False)


class ArtifactReviewAttempt(Base):
    """Immutable per-reviewer-call record; full history survives beyond item status."""

    __tablename__ = "artifact_review_attempts"
    __table_args__ = (
        UniqueConstraint("review_item_id", "attempt", name="uq_artifact_review_attempt"),
        CheckConstraint(
            "decision IN ('approve','revise','reject')",
            name="ck_artifact_review_attempts_decision",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    review_item_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("artifact_review_items.id", ondelete="CASCADE"), nullable=False
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    draft_revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("artifact_draft_revisions.id"), nullable=False
    )
    review_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_runs.id"), nullable=False
    )
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    issues_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class AgentArtifactVersion(Base):
    """Immutable published version; never updated after publication."""

    __tablename__ = "agent_artifact_versions"
    __table_args__ = (
        UniqueConstraint("artifact_id", "version", name="uq_agent_artifact_versions_artifact_version"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    artifact_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_artifacts.id"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_runs.id"), nullable=False
    )
    source_draft_revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("artifact_draft_revisions.id"), nullable=False
    )
    parent_artifact_version_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_artifact_versions.id"), nullable=True
    )
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    evidence_refs_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    # 发布时冻结的 Evidence 传递闭包审计快照（设计 §5.6）；旧 Version 为 NULL，
    # 写入逻辑由后续任务落地。evidence_refs_json 仍记录模型直接引用。
    lineage_snapshot_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    review_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # 直接发布时冻结的确定性校验快照（迁移 0030）；旧 Version 为 NULL。
    validation_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    data_status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ArtifactPublishAttempt(Base):
    """Immutable per-publish-attempt record (direct publish, no reviewer model)."""

    __tablename__ = "artifact_publish_attempts"
    __table_args__ = (
        # 终态聚合按 run_id 扫描（engine._publish_outcome_artifact_ids）。
        Index("ix_artifact_publish_attempts_run_id", "run_id"),
        UniqueConstraint(
            "idempotency_key", name="uq_artifact_publish_attempts_idempotency"
        ),
        CheckConstraint(
            "status IN ('validating','published','validation_failed','failed')",
            name="ck_artifact_publish_attempts_status",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_runs.id"), nullable=False
    )
    # 引用失败（draft 不存在等）无法确定 artifact_id/draft_revision_id，
    # 允许 NULL；正常发布与拒绝记录都落此表参与终态聚合。
    artifact_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_artifacts.id"), nullable=True
    )
    draft_revision_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("artifact_draft_revisions.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="validating")
    idempotency_key: Mapped[str] = mapped_column(String(191), nullable=False)
    validation_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    published_version_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_artifact_versions.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ArtifactEvent(Base):
    __tablename__ = "artifact_events"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence", name="uq_artifact_events_session_sequence"),
        CheckConstraint(
            "event_type IN ('draft_created','draft_updated','reviewing','published','failed')",
            name="ck_artifact_events_event_type",
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
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    module: Mapped[str] = mapped_column(String(32), nullable=False)
    artifact_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_artifacts.id"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    draft_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    artifact_version_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_artifact_versions.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class AgentArtifactReadState(Base):
    """Agent 会话的模块已读水位（设计 §8.1 read cursor，驱动 BI 未读圆点）。

    独立新表（迁移 0028）：session_id FK 指向 ``agent_sessions.id``。遗留
    ``artifact_read_states``（app.artifacts.models.ArtifactReadState，FK 指向旧
    ``sessions``）保持不动，仅供旧应用版本回滚使用，新代码路径不再读写。
    """

    __tablename__ = "agent_artifact_read_states"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "session_id",
            "module",
            name="uq_agent_artifact_read_states_user_session_module",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False
    )
    module: Mapped[str] = mapped_column(String(32), nullable=False)
    last_seen_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class KolDetailCache(Base):
    __tablename__ = "kol_detail_cache"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "session_id",
            "platform",
            "kol_uid",
            name="uq_kol_detail_cache_user_session_platform_kol",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False
    )
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    kol_uid: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    evidence_refs_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ArtifactExport(Base):
    """Excel 导出缓存行（Gate C Task 6）：同一 Version+模板只构建一次。

    status: building/ready/failed；唯一约束 (artifact_version_id,
    template_version) 串行化并发；失败行可安全重试（覆盖为 building）。
    """

    __tablename__ = "artifact_exports"
    __table_args__ = (
        UniqueConstraint(
            "artifact_version_id",
            "template_version",
            name="uq_artifact_exports_version_template",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    artifact_version_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agent_artifact_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    template_version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    storage_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
