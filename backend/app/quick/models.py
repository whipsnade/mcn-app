from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


QUICK_FEATURES = ("kol_recommend", "kol_detail", "top_posts")
QUICK_CALL_STATUSES = ("running", "succeeded", "failed")


class QuickMcpCall(Base):
    """快捷功能（2x2 按钮）同步 MCP 调用的留痕与积分台账关联。"""

    __tablename__ = "quick_mcp_calls"
    __table_args__ = (
        Index("ix_quick_mcp_calls_user_created", "user_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    feature: Mapped[str] = mapped_column(String(24), nullable=False)
    internal_tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    arguments_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    points_cost: Mapped[int] = mapped_column(Integer, nullable=False)
    reserve_transaction_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("wallet_transactions.id")
    )
    settlement_transaction_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("wallet_transactions.id")
    )
    error_type: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
