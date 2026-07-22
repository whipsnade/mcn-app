from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SessionKolSelection(Base):
    """Session-scoped KOL shortlist, deduplicated by (session_id, platform, kol_uid).

    Replaces the old pipeline's task_candidates system: selections accumulate
    across agent tasks within a session instead of being recomputed per task.
    """

    __tablename__ = "session_kol_selections"
    __table_args__ = (
        UniqueConstraint(
            "session_id", "platform", "kol_uid", name="uq_kol_selection_session_platform_uid"
        ),
        Index("ix_kol_selection_user", "user_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    kol_uid: Mapped[str] = mapped_column(String(128), nullable=False)
    nickname: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    followers: Mapped[int | None] = mapped_column(BigInteger)
    city: Mapped[str | None] = mapped_column(String(64))
    profile_url: Mapped[str | None] = mapped_column(String(512))
    fields_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    score_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    source_tool: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    first_task_id: Mapped[str] = mapped_column(String(36), nullable=False, default="")
    last_task_id: Mapped[str] = mapped_column(String(36), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
