from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
import app.goals.models  # noqa: F401  # task_artifacts.goal_id 的 FK 目标表需先注册


class TaskArtifact(Base):
    """Registered output of a goal/task (report, selection set, ...), keyed for idempotent upsert."""

    __tablename__ = "task_artifacts"
    __table_args__ = (
        UniqueConstraint("artifact_key", name="uq_task_artifacts_artifact_key"),
        Index("ix_task_artifacts_session_type", "session_id", "artifact_type"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    goal_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("task_goals.id"), nullable=True
    )
    artifact_key: Mapped[str] = mapped_column(String(191), nullable=False)
    artifact_type: Mapped[str] = mapped_column(String(48), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    report_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("analysis_reports.id"), nullable=True
    )
    selection_set_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("kol_selection_sets.id"), nullable=True
    )
    scope_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ArtifactReadState(Base):
    """Per-user per-module read cursor for artifact unread hints (wired in stage 3)."""

    __tablename__ = "artifact_read_states"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "session_id",
            "module_key",
            name="uq_artifact_read_states_user_session_module",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    module_key: Mapped[str] = mapped_column(String(32), nullable=False)
    last_seen_artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
