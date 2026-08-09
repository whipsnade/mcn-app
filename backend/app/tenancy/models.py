from datetime import datetime
from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


SUPPORTED_LICENSE_FEATURES = frozenset(
    {"kol_selection", "brand_analysis", "campaign_analysis", "kol_detail", "utility"}
)


class Tenant(Base):
    __tablename__ = "tenants"
    __table_args__ = (
        CheckConstraint("status IN ('active','disabled')", name="ck_tenants_status"),
        CheckConstraint("runtime_backend IN ('current','pi')", name="ck_tenants_runtime_backend"),
        CheckConstraint("license_status IN ('active','suspended')", name="ck_tenants_license_status"),
        UniqueConstraint("slug", name="uq_tenants_slug"),
        Index("ix_tenants_status_runtime", "status", "runtime_backend"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    is_internal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    runtime_backend: Mapped[str] = mapped_column(String(16), nullable=False, default="current")
    license_status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    active_license_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tenant_licenses.id", ondelete="SET NULL"), nullable=True
    )
    active_runtime_config_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("runtime_config_versions.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class TenantMembership(Base):
    __tablename__ = "tenant_memberships"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="uq_tenant_memberships_tenant_user"),
        UniqueConstraint("user_id", name="uq_tenant_memberships_user"),
        CheckConstraint("role IN ('owner','admin','member')", name="ck_tenant_memberships_role"),
        CheckConstraint("status IN ('active','disabled')", name="ck_tenant_memberships_status"),
        Index("ix_tenant_memberships_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="member")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
