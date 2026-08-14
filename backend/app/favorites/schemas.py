"""收藏 API 的请求/响应 Schema。

收藏契约不变：旧 reporting 包中的 FavoriteCreate/FavoriteRead 原样迁入。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator


class FavoriteCreate(BaseModel):
    kol_id: str | None = Field(default=None, min_length=1, max_length=36)
    platform: str | None = Field(default=None, min_length=1, max_length=32)
    kol_uid: str | None = Field(default=None, min_length=1, max_length=128)
    nickname: str = Field(default="", max_length=200)
    snapshot: dict[str, Any] | None = None
    note: str | None = Field(default=None, max_length=500)
    source_task_id: str | None = Field(default=None, min_length=1, max_length=36)

    @model_validator(mode="after")
    def _identity_exactly_one(self) -> FavoriteCreate:
        by_key = self.platform is not None or self.kol_uid is not None
        if self.kol_id is not None and by_key:
            raise ValueError("kol_id 与 platform/kol_uid 不两立")
        if self.kol_id is None:
            if not by_key:
                raise ValueError("kol_id 或 platform+kol_uid 必居其一")
            if self.platform is None or self.kol_uid is None:
                raise ValueError("platform 与 kol_uid 需同时提供")
        return self


class FavoriteRead(BaseModel):
    id: str
    kol_id: str | None = None
    nickname: str | None = None
    platform: str
    kol_uid: str | None = None
    snapshot: dict[str, Any] | None = None
    platform_account_id: str | None = None
    profile_url: str | None = None
    note: str | None = None
    source_task_id: str | None = None
    created_at: datetime
