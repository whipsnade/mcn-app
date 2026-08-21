from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

GLOBAL_SCOPE_KEY = "__global__"


class SkillRevision(Base):
    """An immutable, validated Markdown Skill revision.

    The database stores the complete content so a worker never has to read a
    mutable checkout or a user's local ``.pi/skills`` directory.
    """

    __tablename__ = "skill_revisions"
    __table_args__ = (
        UniqueConstraint(
            "scope_key",
            "skill_name",
            "revision",
            name="uq_skill_revisions_tenant_name_revision",
        ),
        CheckConstraint("revision > 0", name="ck_skill_revisions_revision_positive"),
        Index("ix_skill_revisions_name_created", "skill_name", "created_at"),
        Index("ix_skill_revisions_tenant_name", "tenant_id", "skill_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=True
    )
    scope_key: Mapped[str] = mapped_column(
        String(36), nullable=False, default=GLOBAL_SCOPE_KEY
    )
    skill_name: Mapped[str] = mapped_column(String(96), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(String(512), nullable=False)
    required_tools: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    artifact_contract: Mapped[str | None] = mapped_column(String(96), nullable=True)
    created_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    change_note: Mapped[str | None] = mapped_column(String(512), nullable=True)


class SkillActivation(Base):
    """A mutable pointer selecting a previously stored Skill revision."""

    __tablename__ = "skill_activations"
    __table_args__ = (
        UniqueConstraint(
            "environment",
            "scope_key",
            "skill_name",
            name="uq_skill_activations_environment_tenant_name",
        ),
        CheckConstraint(
            "rollout_percent >= 0 AND rollout_percent <= 100",
            name="ck_skill_activations_rollout_percent",
        ),
        CheckConstraint(
            "previous_rollout_percent IS NULL OR "
            "(previous_rollout_percent >= 0 AND previous_rollout_percent <= 100)",
            name="ck_skill_activations_previous_rollout_percent",
        ),
        Index("ix_skill_activations_environment_name", "environment", "skill_name"),
        Index("ix_skill_activations_tenant_name", "tenant_id", "skill_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    environment: Mapped[str] = mapped_column(String(24), nullable=False)
    tenant_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True
    )
    scope_key: Mapped[str] = mapped_column(
        String(36), nullable=False, default=GLOBAL_SCOPE_KEY
    )
    skill_name: Mapped[str] = mapped_column(String(96), nullable=False)
    active_revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("skill_revisions.id", ondelete="RESTRICT"), nullable=False
    )
    previous_revision_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("skill_revisions.id", ondelete="RESTRICT"), nullable=True
    )
    rollout_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    previous_rollout_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


@event.listens_for(SkillRevision, "before_update")
def _reject_skill_revision_update(_mapper: Any, _connection: Any, _target: SkillRevision) -> None:
    raise ValueError("skill_revision_immutable")


@event.listens_for(SkillRevision, "before_delete")
def _reject_skill_revision_delete(_mapper: Any, _connection: Any, _target: SkillRevision) -> None:
    raise ValueError("skill_revision_immutable")


def _sync_scope_key(_mapper: Any, _connection: Any, target: SkillRevision | SkillActivation) -> None:
    target.scope_key = target.tenant_id or GLOBAL_SCOPE_KEY


event.listen(SkillRevision, "before_insert", _sync_scope_key)
event.listen(SkillActivation, "before_insert", _sync_scope_key)
event.listen(SkillActivation, "before_update", _sync_scope_key)


__all__ = ["GLOBAL_SCOPE_KEY", "SkillActivation", "SkillRevision"]
