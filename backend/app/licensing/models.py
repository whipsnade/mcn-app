from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, JSON, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.db.base import Base
from app.tenancy.models import SUPPORTED_LICENSE_FEATURES


class TenantLicense(Base):
    __tablename__ = "tenant_licenses"
    __table_args__ = (
        UniqueConstraint("tenant_id", "version", name="uq_tenant_licenses_tenant_version"),
        CheckConstraint("version > 0", name="ck_tenant_licenses_version_positive"),
        CheckConstraint("max_concurrent_runs > 0", name="ck_tenant_licenses_max_concurrent"),
        CheckConstraint(
            "max_user_concurrent_runs > 0", name="ck_tenant_licenses_max_user_concurrent"
        ),
        Index("ix_tenant_licenses_tenant_dates", "tenant_id", "valid_from", "valid_until"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    features_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    max_concurrent_runs: Mapped[int] = mapped_column(Integer, nullable=False)
    max_user_concurrent_runs: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    @validates("features_json")
    def validate_features_json(self, _key: str, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict) or not set(value).issubset(SUPPORTED_LICENSE_FEATURES):
            raise ValueError("license_feature_unknown")
        if any(not isinstance(flag, bool) for flag in value.values()):
            raise ValueError("license_feature_flag_invalid")
        return value
