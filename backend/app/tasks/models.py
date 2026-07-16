from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AnalysisTask(Base):
    __tablename__ = "analysis_tasks"
    __table_args__ = (
        Index(
            "ix_analysis_tasks_user_session_created",
            "user_id",
            "session_id",
            "created_at",
        ),
        Index("ix_analysis_tasks_status_lease", "status", "lease_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    trigger_message_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("messages.id"), nullable=False
    )
    retry_of_task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    retry_key: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    plan_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    plan_version: Mapped[str | None] = mapped_column(String(32))
    replan_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    replan_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    estimated_points: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(500))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime)
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class TaskEvent(Base):
    __tablename__ = "task_events"
    __table_args__ = (Index("ix_task_events_task_id_id", "task_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("analysis_tasks.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
