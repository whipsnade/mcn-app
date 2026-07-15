from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ModelRun(Base):
    __tablename__ = "model_runs"
    __table_args__ = (Index("ix_model_runs_task_created", "task_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("analysis_tasks.id", ondelete="CASCADE"), nullable=False
    )
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_template: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    cached_tokens: Mapped[int | None] = mapped_column(Integer)
    reasoning_tokens: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error_type: Mapped[str | None] = mapped_column(String(64))
    request_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
