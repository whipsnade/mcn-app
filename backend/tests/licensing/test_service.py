from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.agent_runtime.models import AgentRun, AgentSession
from app.identity.models import User
from app.licensing.models import TenantLicense
from app.licensing.service import LicenseService
from app.tenancy.models import Tenant, TenantMembership


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _seed_tenant(
    db_session,
    *,
    valid_from: datetime,
    valid_until: datetime | None,
    max_concurrent_runs: int = 2,
    max_user_concurrent_runs: int = 1,
) -> tuple[str, str]:
    now = _now()
    user_id = str(uuid4())
    tenant_id = str(uuid4())
    license_id = str(uuid4())
    db_session.add(
        User(
            id=user_id,
            nickname="授权测试",
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
            name="授权租户",
            status="active",
            is_internal=False,
            runtime_backend="current",
            license_status="active",
            active_license_id=None,
            created_at=now,
            updated_at=now,
        )
    )
    await db_session.flush()
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
    db_session.add(
        TenantLicense(
            id=license_id,
            tenant_id=tenant_id,
            version=1,
            valid_from=valid_from,
            valid_until=valid_until,
            features_json={"brand_analysis": True},
            max_concurrent_runs=max_concurrent_runs,
            max_user_concurrent_runs=max_user_concurrent_runs,
            created_by=user_id,
            created_at=now,
        )
    )
    await db_session.flush()
    tenant = await db_session.get(Tenant, tenant_id)
    assert tenant is not None
    tenant.active_license_id = license_id
    await db_session.flush()
    return tenant_id, user_id


@pytest.mark.asyncio
async def test_authorize_run_accepts_active_feature(db_session) -> None:
    now = _now()
    tenant_id, user_id = await _seed_tenant(
        db_session,
        valid_from=now - timedelta(minutes=1),
        valid_until=now + timedelta(hours=1),
    )

    decision = await LicenseService(db_session).authorize_run(
        tenant_id, user_id, "brand_analysis"
    )

    assert decision.allowed is True

    assert decision.code == "ok"
    assert decision.max_tenant_concurrency == 2
    assert decision.max_user_concurrency == 1


@pytest.mark.asyncio
async def test_authorize_run_rejects_expired_license(db_session) -> None:
    now = _now()
    tenant_id, user_id = await _seed_tenant(
        db_session,
        valid_from=now - timedelta(hours=2),
        valid_until=now - timedelta(minutes=1),
    )

    decision = await LicenseService(db_session).authorize_run(
        tenant_id, user_id, "brand_analysis"
    )

    assert decision.allowed is False
    assert decision.code == "license_expired"


@pytest.mark.asyncio
async def test_authorize_run_rejects_not_started_and_missing_feature(db_session) -> None:
    now = _now()
    tenant_id, user_id = await _seed_tenant(
        db_session,
        valid_from=now + timedelta(minutes=1),
        valid_until=None,
    )

    decision = await LicenseService(db_session).authorize_run(
        tenant_id, user_id, "brand_analysis"
    )

    assert decision.allowed is False
    assert decision.code == "license_not_started"

    tenant_id, user_id = await _seed_tenant(
        db_session,
        valid_from=now - timedelta(minutes=1),
        valid_until=None,
    )
    decision = await LicenseService(db_session).authorize_run(
        tenant_id, user_id, "campaign_analysis"
    )
    assert decision.allowed is False
    assert decision.code == "feature_disabled"


@pytest.mark.asyncio
async def test_authorize_run_counts_only_same_tenant(db_session) -> None:
    now = _now()
    tenant_id, user_id = await _seed_tenant(
        db_session,
        valid_from=now - timedelta(minutes=1),
        valid_until=None,
        max_concurrent_runs=1,
    )
    other_tenant_id, other_user_id = await _seed_tenant(
        db_session,
        valid_from=now - timedelta(minutes=1),
        valid_until=None,
        max_concurrent_runs=1,
    )
    other_session = AgentSession(
        id=str(uuid4()),
        user_id=other_user_id,
        tenant_id=other_tenant_id,
        title="other",
        status="active",
        created_at=now,
        updated_at=now,
    )
    db_session.add(other_session)
    await db_session.flush()
    db_session.add(
        AgentRun(
            id=str(uuid4()),
            session_id=other_session.id,
            user_id=other_user_id,
            tenant_id=other_tenant_id,
            run_kind="user",
            visibility="user",
            profile_name="session_analyst_v1",
            profile_version="v1",
            model="test",
            status="running",
            decision_count=0,
            review_count=0,
            revision_count=0,
            created_at=now,
        )
    )
    await db_session.flush()

    decision = await LicenseService(db_session).authorize_run(
        tenant_id, user_id, "brand_analysis"
    )

    assert decision.allowed is True

    own_session = AgentSession(
        id=str(uuid4()),
        user_id=user_id,
        tenant_id=tenant_id,
        title="own",
        status="active",
        created_at=now,
        updated_at=now,
    )
    db_session.add(own_session)
    await db_session.flush()
    db_session.add(
        AgentRun(
            id=str(uuid4()),
            session_id=own_session.id,
            user_id=user_id,
            tenant_id=tenant_id,
            run_kind="user",
            visibility="user",
            profile_name="session_analyst_v1",
            profile_version="v1",
            model="test",
            status="running",
            decision_count=0,
            review_count=0,
            revision_count=0,
            created_at=now,
        )
    )
    await db_session.flush()
    denied = await LicenseService(db_session).authorize_run(
        tenant_id, user_id, "brand_analysis"
    )
    assert denied.allowed is False
    assert denied.code == "tenant_concurrency_exceeded"
