from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.models import AdminAuditLog
from app.admin.schemas import (
    AdminUserCreate,
    AdminUserItem,
    AdminUserUpdate,
    PointsHistoryEntry,
)
from app.billing.models import Wallet, WalletTransaction
from app.billing.service import WalletService
from app.core.redaction import redact_for_log
from app.identity.models import AuthIdentity, LoginSession, User, UserChannelPermission
from app.mcp_gateway.models import McpCall
from app.tasks.models import AnalysisTask
from app.workspace.models import WorkspaceSession


HISTORY_KINDS = ("settle", "admin_adjust", "welcome_grant")


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class PhoneConflictError(Exception):
    """Raised when a phone number already belongs to another account."""


class AdminService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _get_user(self, user_id: str) -> User:
        user = await self.db.get(User, user_id)
        if user is None:
            raise LookupError("user_not_found")
        return user

    async def _phone_of(self, user_id: str) -> str | None:
        return await self.db.scalar(
            select(AuthIdentity.provider_subject).where(
                AuthIdentity.provider == "sms",
                AuthIdentity.user_id == user_id,
            )
        )

    async def _channels_of(self, user_id: str) -> list[str]:
        return list(
            (
                await self.db.scalars(
                    select(UserChannelPermission.channel).where(
                        UserChannelPermission.user_id == user_id,
                        UserChannelPermission.is_enabled.is_(True),
                    )
                )
            ).all()
        )

    async def _to_item(self, user: User) -> AdminUserItem:
        wallet = await self.db.get(Wallet, user.id)
        return AdminUserItem(
            id=user.id,
            nickname=user.nickname,
            role=user.role,
            status=user.status,
            phone=await self._phone_of(user.id),
            points=wallet.balance if wallet is not None else 0,
            reserved_points=wallet.reserved if wallet is not None else 0,
            channels=await self._channels_of(user.id),
            created_at=user.created_at,
        )

    def _audit(
        self,
        admin_id: str,
        *,
        action: str,
        target_type: str,
        target_id: str,
        detail: dict[str, Any],
    ) -> AdminAuditLog:
        entry = AdminAuditLog(
            id=str(uuid4()),
            admin_user_id=admin_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            detail_json=redact_for_log(detail),
            created_at=utc_now(),
        )
        self.db.add(entry)
        return entry

    async def list_users(
        self,
        *,
        keyword: str | None,
        channel: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[AdminUserItem], int]:
        statement = select(User)
        if keyword:
            like = f"%{keyword}%"
            phone_matches = select(AuthIdentity.user_id).where(
                AuthIdentity.provider == "sms",
                AuthIdentity.provider_subject.like(like),
            )
            statement = statement.where(
                or_(User.nickname.like(like), User.id.in_(phone_matches))
            )
        if channel:
            channel_matches = select(UserChannelPermission.user_id).where(
                UserChannelPermission.channel == channel,
                UserChannelPermission.is_enabled.is_(True),
            )
            statement = statement.where(User.id.in_(channel_matches))
        total = await self.db.scalar(
            select(func.count()).select_from(statement.subquery())
        )
        users = list(
            (
                await self.db.scalars(
                    statement.order_by(User.created_at.desc(), User.id)
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
        )
        return [await self._to_item(user) for user in users], total or 0

    async def create_user(self, admin: User, payload: AdminUserCreate) -> AdminUserItem:
        conflict_id = await self.db.scalar(
            select(AuthIdentity.user_id).where(
                AuthIdentity.provider == "sms",
                AuthIdentity.provider_subject == payload.phone,
            )
        )
        if conflict_id is not None:
            raise PhoneConflictError()
        now = utc_now()
        user = User(
            id=str(uuid4()),
            nickname=payload.nickname,
            role=payload.role,
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
                provider="sms",
                provider_subject=payload.phone,
                created_at=now,
                updated_at=now,
            )
        )
        for channel in payload.channels:
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
        audit = self._audit(
            admin.id,
            action="user.create",
            target_type="user",
            target_id=user.id,
            detail={
                "after": {
                    "nickname": user.nickname,
                    "phone": payload.phone,
                    "role": user.role,
                    "channels": payload.channels,
                    "points": payload.points,
                }
            },
        )
        await self.db.flush()
        if payload.points > 0:
            await WalletService(self.db).admin_adjust(
                user.id,
                delta=payload.points,
                reason="initial grant",
                idempotency_key=f"admin-create:{user.id}",
                reference_id=audit.id,
            )
        await self.db.flush()
        return await self._to_item(user)

    async def update_user(
        self, admin: User, user_id: str, payload: AdminUserUpdate
    ) -> AdminUserItem:
        user = await self._get_user(user_id)
        if user.id == admin.id:
            if payload.role is not None and payload.role != "admin":
                raise ValueError("self_role_change_forbidden")
            if payload.status is not None and payload.status != "active":
                raise ValueError("self_disable_forbidden")
        before = {
            "nickname": user.nickname,
            "phone": await self._phone_of(user.id),
            "role": user.role,
            "status": user.status,
            "channels": await self._channels_of(user.id),
        }
        if payload.phone is not None and payload.phone != before["phone"]:
            conflict_id = await self.db.scalar(
                select(AuthIdentity.user_id).where(
                    AuthIdentity.provider == "sms",
                    AuthIdentity.provider_subject == payload.phone,
                    AuthIdentity.user_id != user.id,
                )
            )
            if conflict_id is not None:
                raise PhoneConflictError()
            identity = await self.db.scalar(
                select(AuthIdentity).where(
                    AuthIdentity.provider == "sms",
                    AuthIdentity.user_id == user.id,
                )
            )
            now = utc_now()
            if identity is None:
                self.db.add(
                    AuthIdentity(
                        id=str(uuid4()),
                        user_id=user.id,
                        provider="sms",
                        provider_subject=payload.phone,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                identity.provider_subject = payload.phone
                identity.updated_at = now
        if payload.nickname is not None:
            user.nickname = payload.nickname
        if payload.role is not None:
            user.role = payload.role
        if payload.status is not None:
            user.status = payload.status
        user.updated_at = utc_now()
        if payload.channels is not None:
            existing = list(
                (
                    await self.db.scalars(
                        select(UserChannelPermission).where(
                            UserChannelPermission.user_id == user.id
                        )
                    )
                ).all()
            )
            for row in existing:
                await self.db.delete(row)
            # Flush the deletes before re-adding rows so the (user_id, channel)
            # unique constraint is not violated when a channel is kept.
            await self.db.flush()
            now = utc_now()
            for channel in payload.channels:
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
        after = {
            "nickname": user.nickname,
            "phone": payload.phone if payload.phone is not None else before["phone"],
            "role": user.role,
            "status": user.status,
            "channels": (
                payload.channels if payload.channels is not None else before["channels"]
            ),
        }
        self._audit(
            admin.id,
            action="user.update",
            target_type="user",
            target_id=user.id,
            detail={"before": before, "after": after},
        )
        await self.db.flush()
        return await self._to_item(user)

    async def disable_user(self, admin: User, user_id: str) -> None:
        if user_id == admin.id:
            raise ValueError("self_delete_forbidden")
        user = await self._get_user(user_id)
        now = utc_now()
        user.status = "disabled"
        user.updated_at = now
        sessions = list(
            (
                await self.db.scalars(
                    select(LoginSession).where(
                        LoginSession.user_id == user.id,
                        LoginSession.revoked_at.is_(None),
                    )
                )
            ).all()
        )
        for session in sessions:
            session.revoked_at = now
        self._audit(
            admin.id,
            action="user.disable",
            target_type="user",
            target_id=user.id,
            detail={
                "before": {"status": "active"},
                "after": {"status": "disabled"},
                "revoked_sessions": len(sessions),
            },
        )
        await self.db.flush()

    async def adjust_points(
        self,
        admin: User,
        user_id: str,
        *,
        delta: int,
        reason: str,
        idempotency_key: str | None,
    ) -> tuple[Wallet, WalletTransaction]:
        user = await self._get_user(user_id)
        wallet_service = WalletService(self.db)
        if idempotency_key is not None:
            applied = await self.db.scalar(
                select(WalletTransaction).where(
                    WalletTransaction.idempotency_key == idempotency_key
                )
            )
            if applied is not None:
                return await wallet_service.get_wallet(user.id), applied
        audit = self._audit(
            admin.id,
            action="points.adjust",
            target_type="wallet",
            target_id=user.id,
            detail={
                "delta": delta,
                "reason": reason,
                "phone": await self._phone_of(user.id),
            },
        )
        await self.db.flush()
        wallet, transaction = await wallet_service.admin_adjust(
            user.id,
            delta=delta,
            reason=reason,
            idempotency_key=idempotency_key or f"admin-adjust:{uuid4()}",
            reference_id=audit.id,
        )
        audit.detail_json = redact_for_log(
            {
                "delta": delta,
                "reason": reason,
                "phone": await self._phone_of(user.id),
                "balance_after": wallet.balance,
            }
        )
        await self.db.flush()
        return wallet, transaction

    async def points_history(
        self, user_id: str, *, limit: int, offset: int
    ) -> tuple[list[PointsHistoryEntry], int]:
        await self._get_user(user_id)
        statement = select(WalletTransaction).where(
            WalletTransaction.user_id == user_id,
            WalletTransaction.kind.in_(HISTORY_KINDS),
        )
        total = await self.db.scalar(
            select(func.count()).select_from(statement.subquery())
        )
        transactions = list(
            (
                await self.db.scalars(
                    statement.order_by(
                        WalletTransaction.created_at.desc(), WalletTransaction.id
                    )
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
        )
        settle_ids = [tx.id for tx in transactions if tx.kind == "settle"]
        context: dict[str, tuple[str | None, str | None]] = {}
        if settle_ids:
            rows = (
                await self.db.execute(
                    select(
                        McpCall.settlement_transaction_id,
                        McpCall.service_slug,
                        WorkspaceSession.title,
                        WorkspaceSession.platforms,
                    )
                    .join(AnalysisTask, McpCall.task_id == AnalysisTask.id)
                    .join(WorkspaceSession, AnalysisTask.session_id == WorkspaceSession.id)
                    .where(McpCall.settlement_transaction_id.in_(settle_ids))
                )
            ).all()
            for tx_id, service_slug, title, platforms in rows:
                platform = platforms[0] if platforms else service_slug
                context[tx_id] = (title, platform)
        items = [
            PointsHistoryEntry(
                id=tx.id,
                kind=tx.kind,
                points=-tx.reserved_delta if tx.kind == "settle" else tx.balance_delta,
                session_title=context.get(tx.id, (None, None))[0],
                platform=context.get(tx.id, (None, None))[1],
                created_at=tx.created_at,
            )
            for tx in transactions
        ]
        return items, total or 0
