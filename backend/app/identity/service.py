from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.service import WalletService
from app.core.config import get_settings
from app.core.security import create_access_token, create_refresh_token, hash_refresh_token
from app.identity.models import AuthIdentity, LoginSession, User, UserChannelPermission
from app.tenancy.service import TenantService


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
        identity = await self._find_identity(provider, subject)
        if identity is None:
            try:
                user = await self._create_user(provider, subject, nickname)
            except IntegrityError:
                # 并发首登收敛：同一 subject 的另一请求抢先提交，本请求在
                # auth_identities (provider, subject) 唯一键上竞争失败。回滚
                # 本事务（丢弃本方部分建户写入）后重读既有身份，按已存在
                # 用户正常返回登录会话——不重试 INSERT、不造重复用户；
                # 钱包 1000 积分赠送等建户路径只归首建者。
                await self.db.rollback()
                identity = await self._find_identity(provider, subject)
                if identity is None:
                    raise
                user = await self._get_active_user(identity.user_id)
        else:
            user = await self._get_active_user(identity.user_id)

        await self._ensure_default_channels(user.id)
        await WalletService(self.db).ensure_welcome_grant(user.id)
        return await self._create_login_session(user)

    async def _find_identity(self, provider: str, subject: str) -> AuthIdentity | None:
        statement = select(AuthIdentity).where(
            AuthIdentity.provider == provider,
            AuthIdentity.provider_subject == subject,
        )
        return await self.db.scalar(statement)

    async def _get_active_user(self, user_id: str) -> User:
        user = await self.db.get(User, user_id)
        if user is None or user.status != "active":
            raise PermissionError("user_inactive")
        return user

    async def _ensure_default_channels(self, user_id: str) -> None:
        existing_rows = list(
            (
                await self.db.scalars(
                    select(UserChannelPermission).where(
                        UserChannelPermission.user_id == user_id
                    )
                )
            ).all()
        )
        existing = {row.channel: row for row in existing_rows}
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
            elif not existing[channel].is_enabled:
                existing[channel].is_enabled = True
                existing[channel].updated_at = now
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
        tenant_context = await TenantService(self.db).provision_personal_tenant(
            user.id, name=nickname, now=now
        )
        # New users created after B4 start on the tenant ledger.  The legacy
        # Wallet row is not created as a second source of truth.
        from app.pi_gateway.accounting import TenantAccountingService

        accounting = TenantAccountingService(self.db)
        await accounting.ensure_tenant_wallet(tenant_context.tenant_id)
        await accounting.ensure_user_quota(tenant_context.tenant_id, user.id)
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
