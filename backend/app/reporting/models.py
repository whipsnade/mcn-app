from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, Numeric, String, Text
from sqlalchemy import UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Kol(Base):
    __tablename__ = "kols"
    __table_args__ = (
        UniqueConstraint(
            "platform", "platform_account_id", name="uq_kols_platform_account"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    platform_account_id: Mapped[str] = mapped_column(String(128), nullable=False)
    normalized_profile_url: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class KolSnapshot(Base):
    __tablename__ = "kol_snapshots"
    __table_args__ = (Index("ix_kol_snapshots_kol_collected", "kol_id", "collected_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kol_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("kols.id", ondelete="CASCADE"), nullable=False
    )
    source_mcp_call_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("mcp_calls.id", ondelete="SET NULL")
    )
    normalized_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    collected_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class TaskCandidate(Base):
    __tablename__ = "task_candidates"
    __table_args__ = (
        UniqueConstraint(
            "task_id", "candidate_version", "kol_id", name="uq_task_candidates_version_kol"
        ),
        Index(
            "ix_task_candidates_task_version_rank", "task_id", "candidate_version", "rank"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("analysis_tasks.id", ondelete="CASCADE"), nullable=False
    )
    kol_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("kols.id", ondelete="CASCADE"), nullable=False
    )
    snapshot_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("kol_snapshots.id"), nullable=False
    )
    candidate_version: Mapped[int] = mapped_column(Integer, nullable=False)
    total_score: Mapped[Decimal] = mapped_column(Numeric(6, 3), nullable=False)
    score_breakdown_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    matched_conditions_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    risk_flags_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    recommendation_text: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class TaskCandidatePool(Base):
    __tablename__ = "task_candidate_pools"
    __table_args__ = (
        UniqueConstraint("task_id", "pool_version", name="uq_task_candidate_pools_task_version"),
        Index("ix_task_candidate_pools_task_version", "task_id", "pool_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("analysis_tasks.id", ondelete="CASCADE"), nullable=False
    )
    pool_version: Mapped[int] = mapped_column(Integer, nullable=False)
    field_contract_version: Mapped[str] = mapped_column(String(32), nullable=False)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class TaskCandidatePoolItem(Base):
    __tablename__ = "task_candidate_pool_items"
    __table_args__ = (
        UniqueConstraint("pool_id", "kol_id", name="uq_task_candidate_pool_items_pool_kol"),
        Index("ix_task_candidate_pool_items_pool_rank", "pool_id", "full_rank"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    pool_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("task_candidate_pools.id", ondelete="CASCADE"), nullable=False
    )
    kol_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("kols.id", ondelete="CASCADE"), nullable=False
    )
    snapshot_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("kol_snapshots.id"), nullable=False
    )
    full_rank: Mapped[int] = mapped_column(Integer, nullable=False)
    is_shortlisted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    total_score: Mapped[Decimal] = mapped_column(Numeric(6, 3), nullable=False)
    score_breakdown_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    risk_flags_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class BiReport(Base):
    __tablename__ = "bi_reports"
    __table_args__ = (
        UniqueConstraint("task_id", "report_version", name="uq_bi_reports_task_version"),
        Index("ix_bi_reports_session_created", "session_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("analysis_tasks.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    candidate_version: Mapped[int] = mapped_column(Integer, nullable=False)
    report_version: Mapped[int] = mapped_column(Integer, nullable=False)
    chart_data_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    conclusion_text: Mapped[str | None] = mapped_column(Text)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class UserKolFavorite(Base):
    __tablename__ = "user_kol_favorites"
    __table_args__ = (
        UniqueConstraint("user_id", "kol_id", name="uq_user_kol_favorites_user_kol"),
        Index("ix_user_kol_favorites_user_created", "user_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kol_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("kols.id", ondelete="CASCADE"), nullable=False
    )
    note: Mapped[str | None] = mapped_column(String(500))
    source_task_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("analysis_tasks.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
