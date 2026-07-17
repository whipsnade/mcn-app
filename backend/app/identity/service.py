from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.service import WalletService
from app.core.config import get_settings
from app.core.security import create_access_token, create_refresh_token, hash_refresh_token
from app.identity.models import AuthIdentity, LoginSession, User, UserChannelPermission


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass(frozen=True)
class LoginResult:
    user: User
    access_token: str
    refresh_token: str


class IdentityService:
    default_channels = ("xiaohongshu", "douyin", "bilibili", "weibo", "wechat")

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def login(self, *, provider: str, subject: str, nickname: str) -> LoginResult:
        statement = select(AuthIdentity).where(
            AuthIdentity.provider == provider,
            AuthIdentity.provider_subject == subject,
        )
        identity = await self.db.scalar(statement)
        if identity is None:
            user = await self._create_user(provider, subject, nickname)
        else:
            user = await self.db.get(User, identity.user_id)
            if user is None or user.status != "active":
                raise PermissionError("user_inactive")

        await self._ensure_default_channels(user.id)
        await WalletService(self.db).ensure_welcome_grant(user.id)
        return await self._create_login_session(user)

    async def _ensure_default_channels(self, user_id: str) -> None:
        existing = set(
            (
                await self.db.scalars(
                    select(UserChannelPermission.channel).where(
                        UserChannelPermission.user_id == user_id
                    )
                )
            ).all()
        )
        now = utc_now()
        for channel in self.default_channels:
            if channel not in existing:
                self.db.add(
                    UserChannelPermission(
                        id=str(uuid4()),
                        user_id=user_id,
                        channel=channel,
                        is_enabled=True,
                        created_at=now,
                        updated_at=now,
                    )
                )
        await self.db.flush()

    async def _create_user(self, provider: str, subject: str, nickname: str) -> User:
        now = utc_now()
        user = User(
            id=str(uuid4()),
            nickname=nickname,
            role="user",
            status="active",
            created_at=now,
            updated_at=now,
        )
        self.db.add(user)
        await self.db.flush()
        self.db.add(
            AuthIdentity(
                id=str(uuid4()),
                user_id=user.id,
                provider=provider,
                provider_subject=subject,
                created_at=now,
                updated_at=now,
            )
        )
        for channel in self.default_channels:
            self.db.add(
                UserChannelPermission(
                    id=str(uuid4()),
                    user_id=user.id,
                    channel=channel,
                    is_enabled=True,
                    created_at=now,
                    updated_at=now,
                )
            )
        await self.db.flush()
        return user

    async def _create_login_session(self, user: User) -> LoginResult:
        settings = get_settings()
        raw_refresh_token = create_refresh_token()
        login_session = LoginSession(
            id=str(uuid4()),
            user_id=user.id,
            refresh_token_hash=hash_refresh_token(raw_refresh_token),
            expires_at=utc_now() + timedelta(days=settings.refresh_token_days),
            revoked_at=None,
            created_at=utc_now(),
            last_seen_at=utc_now(),
        )
        self.db.add(login_session)
        await self.db.flush()
        return LoginResult(
            user=user,
            access_token=create_access_token(
                user_id=user.id, session_id=login_session.id, role=user.role
            ),
            refresh_token=raw_refresh_token,
        )

    async def refresh(self, raw_refresh_token: str) -> LoginResult:
        statement = (
            select(LoginSession)
            .where(LoginSession.refresh_token_hash == hash_refresh_token(raw_refresh_token))
            .with_for_update()
        )
        login_session = await self.db.scalar(statement)
        now = utc_now()
        if (
            login_session is None
            or login_session.revoked_at is not None
            or login_session.expires_at <= now
        ):
            raise PermissionError("refresh_expired")
        user = await self.db.get(User, login_session.user_id)
        if user is None or user.status != "active":
            raise PermissionError("user_inactive")
        login_session.revoked_at = now
        login_session.last_seen_at = now
        return await self._create_login_session(user)

    async def revoke(self, raw_refresh_token: str) -> None:
        statement = select(LoginSession).where(
            LoginSession.refresh_token_hash == hash_refresh_token(raw_refresh_token)
        )
        login_session = await self.db.scalar(statement)
        if login_session is not None and login_session.revoked_at is None:
            login_session.revoked_at = utc_now()
            await self.db.flush()
