from sqlalchemy import select
import pytest

from app.identity.models import User
from app.identity.service import IdentityService
from app.licensing.models import TenantLicense
from app.tenancy.models import Tenant, TenantMembership


async def test_new_login_provisions_personal_tenant(db_session) -> None:
    result = await IdentityService(db_session).login(
        provider="mock_sms",
        subject="task1-tenant-provisioning",
        nickname="新租户用户",
    )

    user = await db_session.get(User, result.user.id)
    membership = await db_session.scalar(
        select(TenantMembership).where(TenantMembership.user_id == result.user.id)
    )
    assert user is not None
    assert membership is not None
    tenant = await db_session.get(Tenant, membership.tenant_id)
    assert tenant is not None
    assert tenant.runtime_backend == "current"
    assert tenant.license_status == "active"
    license_row = await db_session.get(TenantLicense, tenant.active_license_id)
    assert license_row is not None
    assert license_row.features_json.get("kol_selection") is True


@pytest.mark.asyncio
async def test_me_exposes_tenant_contract(auth_client_factory) -> None:
    client = await auth_client_factory("13800000101")

    response = await client.get("/api/v1/users/me")

    assert response.status_code == 200
    body = response.json()
    assert body["tenant_id"]
    assert body["tenant_name"] == "手机用户_0101的个人租户"
    assert body["membership_role"] == "owner"
