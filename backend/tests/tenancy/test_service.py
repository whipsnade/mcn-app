from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.identity.models import User
from app.tenancy.models import Tenant, TenantMembership
from app.tenancy.service import TenantService


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.mark.asyncio
async def test_resolve_user_returns_active_tenant_membership(db_session) -> None:
    now = _now()
    user_id = str(uuid4())
    tenant_id = str(uuid4())
    db_session.add(
        User(
            id=user_id,
            nickname="租户测试",
            role="user",
            status="active",
            industries=["美食"],
            created_at=now,
            updated_at=now,
        )
    )
    db_session.add(
        Tenant(
            id=tenant_id,
            slug=f"tenant-{tenant_id[:8]}",
            name="租户测试",
            status="active",
            is_internal=False,
            runtime_backend="current",
            license_status="active",
            active_license_id=None,
            created_at=now,
            updated_at=now,
        )
    )
    db_session.add(
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
    await db_session.flush()

    context = await TenantService(db_session).resolve_user(user_id)

    assert context.tenant_id == tenant_id
    assert context.user_id == user_id
    assert context.membership_role == "owner"


@pytest.mark.asyncio
async def test_resolve_user_rejects_disabled_membership(db_session) -> None:
    now = _now()
    user_id = str(uuid4())
    tenant_id = str(uuid4())
    db_session.add(
        User(
            id=user_id,
            nickname="禁用成员",
            role="user",
            status="active",
            industries=["美食"],
            created_at=now,
            updated_at=now,
        )
    )
    db_session.add(
        Tenant(
            id=tenant_id,
            slug=f"tenant-{tenant_id[:8]}",
            name="禁用成员租户",
            status="active",
            is_internal=False,
            runtime_backend="current",
            license_status="active",
            active_license_id=None,
            created_at=now,
            updated_at=now,
        )
    )
    db_session.add(
        TenantMembership(
            id=str(uuid4()),
            tenant_id=tenant_id,
            user_id=user_id,
            role="member",
            status="disabled",
            created_at=now,
            updated_at=now,
        )
    )
    await db_session.flush()

    with pytest.raises(PermissionError, match="tenant_membership_inactive"):
        await TenantService(db_session).resolve_user(user_id)
