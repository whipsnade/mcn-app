"""Database rows for append-only runtime configuration and encrypted secrets."""

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, JSON, String, UniqueConstraint
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RuntimeConfigVersion(Base):
    __tablename__ = "runtime_config_versions"
    __table_args__ = (
        UniqueConstraint("scope", "tenant_id", "version", name="uq_runtime_config_scope_tenant_version"),
        CheckConstraint("scope IN ('system','tenant')", name="ck_runtime_config_scope"),
        CheckConstraint("status IN ('draft','active','retired')", name="ck_runtime_config_status"),
        CheckConstraint("runtime_backend IN ('current','pi')", name="ck_runtime_config_backend"),
        Index("ix_runtime_config_scope_status", "scope", "status", "tenant_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    tenant_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True
    )
    version: Mapped[int] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    runtime_backend: Mapped[str] = mapped_column(String(16), nullable=False)
    runtime_contract_version: Mapped[str] = mapped_column(String(64), nullable=False)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    secret_refs_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class EncryptedRuntimeSecret(Base):
    __tablename__ = "encrypted_runtime_secrets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "secret_kind", "id", name="uq_runtime_secret_identity"),
        CheckConstraint("status IN ('active','retired')", name="ck_runtime_secret_status"),
        Index("ix_runtime_secret_tenant_kind", "tenant_id", "secret_kind"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    secret_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    algorithm: Mapped[str] = mapped_column(String(32), nullable=False, default="AES-256-GCM")
    nonce: Mapped[str] = mapped_column(String(64), nullable=False)
    ciphertext: Mapped[str] = mapped_column(MEDIUMTEXT, nullable=False)
    key_version: Mapped[str] = mapped_column(String(32), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    masked_value: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
