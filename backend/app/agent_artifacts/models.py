from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
# The legacy app.artifacts.models.ArtifactReadState owns the `artifact_read_states`
# table name; it must be registered first so AgentArtifactReadState can extend the
# existing table (extend_existing) rather than collide with it.
import app.artifacts.models  # noqa: F401


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
    evidence_refs_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    parent_artifact_version_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_artifact_versions.id"), nullable=True
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
    evidence_refs_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    review_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    data_status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


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
    """Spec §8.1 read cursor (module/last_seen_sequence) for the BI unread dots.

    Coexists with the legacy ArtifactReadState (module_key/seen_at) on the same
    table via extend_existing; the migration step reconciles the two column sets.
    """

    __tablename__ = "artifact_read_states"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "session_id",
            "module",
            name="uq_artifact_read_states_user_session_module_v2",
        ),
        {"extend_existing": True},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
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
    evidence_refs_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
