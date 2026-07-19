"""一次性脚本：在开发库创建手机号 18680807961 的管理员账号。"""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.identity.models import AuthIdentity, User, UserChannelPermission
from app.identity.service import IdentityService

PHONE = "18680807961"


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def main() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as db:
        existing = await db.scalar(
            select(AuthIdentity).where(
                AuthIdentity.provider == "sms", AuthIdentity.provider_subject == PHONE
            )
        )
        now = utc_now()
        if existing is not None:
            user = await db.get(User, existing.user_id)
            if user is None:
                raise RuntimeError("identity 存在但 user 缺失，数据异常")
            if user.role != "admin":
                user.role = "admin"
                user.updated_at = now
                await db.commit()
                print(f"已存在用户，已提升为管理员: user_id={user.id}")
            else:
                print(f"管理员账号已存在: user_id={user.id}")
        else:
            user = User(
                id=str(uuid4()),
                nickname=f"手机用户_{PHONE[-4:]}",
                role="admin",
                status="active",
                created_at=now,
                updated_at=now,
            )
            db.add(user)
            await db.flush()
            db.add(
                AuthIdentity(
                    id=str(uuid4()),
                    user_id=user.id,
                    provider="sms",
                    provider_subject=PHONE,
                    created_at=now,
                    updated_at=now,
                )
            )
            for channel in IdentityService.default_channels:
                db.add(
                    UserChannelPermission(
                        id=str(uuid4()),
                        user_id=user.id,
                        channel=channel,
                        is_enabled=True,
                        created_at=now,
                        updated_at=now,
                    )
                )
            await db.commit()
            print(f"已创建管理员账号: user_id={user.id}, phone={PHONE}")
    await engine.dispose()


asyncio.run(main())
