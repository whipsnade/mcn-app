from datetime import datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.identity.models import User
from app.licensing.models import TenantLicense
from app.tenancy.models import Tenant, TenantMembership
from app.tenancy.schemas import TenantContext


def effective_runtime_backend(tenant_backend: str, *, kill_switch: bool) -> str:
    """Select the backend for a new Run without mutating tenant/history state."""
    if tenant_backend not in {"current", "pi"}:
        raise ValueError("runtime_backend_invalid")
    return "current" if kill_switch else tenant_backend


class TenantService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def resolve_user(
        self, user_id: str, *, for_update: bool = False
    ) -> TenantContext:
        statement = (
            select(TenantMembership, Tenant)
            .join(Tenant, Tenant.id == TenantMembership.tenant_id)
            .join(User, User.id == TenantMembership.user_id)
            .where(
                TenantMembership.user_id == user_id,
                TenantMembership.status == "active",
                Tenant.status == "active",
                User.status == "active",
            )
        )
        if for_update:
            statement = statement.with_for_update()
        row = (await self.db.execute(statement)).one_or_none()
        if row is None:
            raise PermissionError("tenant_membership_inactive")
        membership, _tenant = row
        return TenantContext(
            tenant_id=membership.tenant_id,
            user_id=user_id,
            membership_role=membership.role,
        )

    async def provision_personal_tenant(
        self, user_id: str, *, name: str, now: datetime
    ) -> TenantContext:
        """Provision the safe one-user default used by identity/admin creation."""
        existing = await self.db.scalar(
            select(TenantMembership).where(TenantMembership.user_id == user_id)
        )
        if existing is not None:
            return await self.resolve_user(user_id)
        tenant_id = str(uuid4())
        license_id = str(uuid4())
        tenant = Tenant(
            id=tenant_id,
            slug=f"tenant-{tenant_id[:24]}",
            name=f"{name}的个人租户",
            status="active",
            is_internal=False,
            runtime_backend="current",
            license_status="active",
            active_license_id=None,
            created_at=now,
            updated_at=now,
        )
        self.db.add(tenant)
        await self.db.flush()
        self.db.add(
            TenantLicense(
                id=license_id,
                tenant_id=tenant_id,
                version=1,
                # MySQL DateTime(0) may round a microsecond value up to the next
                # second; floor the activation timestamp so a just-created user
                # is never rejected by the immediate License check.
                valid_from=now.replace(microsecond=0),
                valid_until=None,
                features_json={
                    "kol_selection": True,
                    "brand_analysis": True,
                    "campaign_analysis": True,
                    "kol_detail": True,
                    "utility": True,
                },
                max_concurrent_runs=4,
                max_user_concurrent_runs=2,
                created_by=user_id,
                created_at=now,
            )
        )
        await self.db.flush()
        tenant.active_license_id = license_id
        self.db.add(
            TenantMembership(
                id=str(uuid4()),
                tenant_id=tenant_id,
                user_id=user_id,
                role="owner",
                status="active",
                created_at=now,
                updated_at=now,
            )
        )
        await self.db.flush()
        return TenantContext(tenant_id=tenant_id, user_id=user_id, membership_role="owner")
