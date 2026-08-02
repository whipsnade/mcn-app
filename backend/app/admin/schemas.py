from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field


class AdminUserItem(BaseModel):
    id: str
    nickname: str
    role: str
    status: str
    phone: str | None
    points: int
    reserved_points: int
    channels: list[str]
    industries: list[str]
    created_at: datetime


class AdminUserListResponse(BaseModel):
    items: list[AdminUserItem]
    total: int


class AdminUserCreate(BaseModel):
    nickname: str = Field(min_length=1, max_length=80)
    phone: str = Field(min_length=1, max_length=32)
    role: str = Field(pattern="^(user|admin)$")
    points: int = Field(default=0, ge=0, le=50000)
    channels: list[str] = Field(default_factory=list)


class AdminUserUpdate(BaseModel):
    nickname: str | None = Field(default=None, min_length=1, max_length=80)
    phone: str | None = Field(default=None, min_length=1, max_length=32)
    role: str | None = Field(default=None, pattern="^(user|admin)$")
    status: str | None = Field(default=None, pattern="^(active|disabled)$")
    channels: list[str] | None = None
    industries: list[Annotated[str, Field(min_length=1, max_length=20)]] | None = Field(
        default=None, max_length=5
    )


class PointsAdjustRequest(BaseModel):
    delta: int
    reason: str = Field(min_length=1, max_length=200)


class PointsAdjustResponse(BaseModel):
    points: int
    reserved_points: int
    transaction_id: str


class PointsHistoryEntry(BaseModel):
    id: str
    kind: str
    points: int
    session_title: str | None
    platform: str | None
    created_at: datetime


class PointsHistoryResponse(BaseModel):
    items: list[PointsHistoryEntry]
    total: int


class AgentToolCallReconcileRequest(BaseModel):
    """管理员核对 result_unknown 的 Agent 工具调用。

    - confirm_success：可确认成功。能取回 payload 时创建 Evidence 并结算，
      否则只结算并标记结果不可用（不得伪造 Evidence）；
    - confirm_failure：确认失败，释放预留；
    - keep_unknown：无法核对，保持 reserved/unknown 并追加核对审计。
    """

    decision: Literal["confirm_success", "confirm_failure", "keep_unknown"]
    note: str | None = Field(default=None, max_length=500)


class AgentToolCallReconcileResponse(BaseModel):
    call_id: str
    status: str
    error_type: str | None = None
    points_reserved: int
    points_settled: int
    evidence_id: str | None = None
