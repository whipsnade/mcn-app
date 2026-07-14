from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


ALLOWED_SERVICE_SLUGS = (
    "insight-cube-mcp",
    "social-grow-mcp",
    "social-grow-content-mcp",
    "aktools-mcp",
    "bilibili-mcp",
)

MCP_CALL_STATUSES = (
    "planned",
    "reserved",
    "running",
    "succeeded",
    "failed",
    "unknown",
    "settled",
    "released",
)

_SERVICE_SLUG_CHECK = "service_slug IN ({})".format(
    ", ".join(f"'{service}'" for service in ALLOWED_SERVICE_SLUGS)
)
_CALL_STATUS_CHECK = "status IN ({})".format(
    ", ".join(f"'{status}'" for status in MCP_CALL_STATUSES)
)


class McpToolCatalog(Base):
    __tablename__ = "mcp_tool_catalog"
    __table_args__ = (
        CheckConstraint(_SERVICE_SLUG_CHECK, name="ck_mcp_tool_catalog_service_slug"),
        UniqueConstraint("internal_tool_name", name="uq_mcp_tool_catalog_internal_name"),
        Index("ix_mcp_tool_catalog_service_enabled", "service_slug", "is_enabled"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    service_slug: Mapped[str] = mapped_column(String(64), nullable=False)
    internal_tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    reviewed_description: Mapped[str] = mapped_column(Text, nullable=False)
    input_schema_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    output_validator_version: Mapped[str] = mapped_column(String(32), nullable=False)
    discovery_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    review_status: Mapped[str] = mapped_column(String(32), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class McpCall(Base):
    __tablename__ = "mcp_calls"
    __table_args__ = (
        CheckConstraint(_SERVICE_SLUG_CHECK, name="ck_mcp_calls_service_slug"),
        CheckConstraint(_CALL_STATUS_CHECK, name="ck_mcp_calls_status"),
        UniqueConstraint("logical_call_id", name="uq_mcp_calls_logical_call_id"),
        UniqueConstraint(
            "task_id", "plan_step_id", "attempt", name="uq_mcp_calls_task_step_attempt"
        ),
        UniqueConstraint(
            "settlement_transaction_id", name="uq_mcp_calls_settlement_transaction"
        ),
        Index("ix_mcp_calls_status_updated", "status", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    logical_call_id: Mapped[str] = mapped_column(String(36), nullable=False)
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("analysis_tasks.id", ondelete="CASCADE"), nullable=False
    )
    batch_no: Mapped[int] = mapped_column(Integer, nullable=False)
    plan_step_id: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    service_slug: Mapped[str] = mapped_column(String(64), nullable=False)
    internal_tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    arguments_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reservation_transaction_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("wallet_transactions.id")
    )
    settlement_transaction_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("wallet_transactions.id")
    )
    upstream_request_id: Mapped[str | None] = mapped_column(String(128))
    protocol_session_digest: Mapped[str | None] = mapped_column(String(64))
    response_hash: Mapped[str | None] = mapped_column(String(64))
    evidence_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_type: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(500))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
