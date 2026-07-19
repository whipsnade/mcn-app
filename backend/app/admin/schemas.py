from datetime import datetime

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
