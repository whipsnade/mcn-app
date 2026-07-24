"""用户品牌配置：默认品牌的设置与查询（阶段二 goal/artifact 基础设施）。

每用户最多一个默认品牌：is_default 非默认行存 NULL，靠 MySQL 唯一索引
(user_id, is_default) 的 NULL 语义兜底；设默认时同事务锁定清其他行。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.identity.models import UserBrandProfile


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class BrandProfileService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def list_brand_profiles(self, user_id: str) -> list[UserBrandProfile]:
        rows = await self._db.scalars(
            select(UserBrandProfile)
            .where(UserBrandProfile.user_id == user_id)
            # created_at 秒级精度可能并列，brand_name 兜底保证顺序确定。
            .order_by(UserBrandProfile.created_at, UserBrandProfile.brand_name)
        )
        return list(rows.all())

    async def set_default_brand(self, user_id: str, brand_name: str) -> UserBrandProfile:
        """设默认品牌：不存在则创建，存在则置默认；同事务清其他行的默认标记。"""
        name = brand_name.strip()
        if not name:
            raise ValueError("brand_name_empty")
        now = _utcnow()
        rows = list(
            (
                await self._db.scalars(
                    select(UserBrandProfile)
                    .where(UserBrandProfile.user_id == user_id)
                    .with_for_update()
                )
            ).all()
        )
        target: UserBrandProfile | None = None
        for row in rows:
            if row.brand_name == name:
                target = row
            elif row.is_default is not None:
                row.is_default = None
                row.updated_at = now
        if target is None:
            target = UserBrandProfile(
                id=str(uuid4()),
                user_id=user_id,
                brand_name=name[:100],
                is_default=True,
                metadata_json=None,
                created_at=now,
                updated_at=now,
            )
            self._db.add(target)
        else:
            target.is_default = True
            target.updated_at = now
        await self._db.flush()
        return target

    async def get_default_brand(self, user_id: str) -> str | None:
        return await self._db.scalar(
            select(UserBrandProfile.brand_name).where(
                UserBrandProfile.user_id == user_id,
                UserBrandProfile.is_default.is_(True),
            )
        )
