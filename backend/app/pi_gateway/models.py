"""Persistence for the authenticated Gateway control plane."""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PiGatewayInstance(Base):
    __tablename__ = "pi_gateway_instances"
    __table_args__ = (
        CheckConstraint("mode IN ('active','draining')", name="ck_pi_gateway_instances_mode"),
        CheckConstraint("status IN ('active','offline','disabled')", name="ck_pi_gateway_instances_status"),
        CheckConstraint("desired_capacity >= 0", name="ck_pi_gateway_instances_capacity"),
        UniqueConstraint("gateway_id", name="uq_pi_gateway_instances_gateway_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    gateway_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    desired_capacity: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class PiGatewayRequestNonce(Base):
    __tablename__ = "pi_gateway_request_nonces"
    __table_args__ = (
        UniqueConstraint("gateway_id", "nonce", name="uq_pi_gateway_nonce_gateway_nonce"),
        Index("ix_pi_gateway_request_nonces_expires", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    gateway_id: Mapped[str] = mapped_column(String(64), nullable=False)
    nonce: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class PiTenantQueueState(Base):
    __tablename__ = "pi_tenant_queue_states"
    __table_args__ = (Index("ix_pi_tenant_queue_states_claimed", "last_claimed_at"),)

    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    last_claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    active_runs: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
