from datetime import datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.agent_runtime.models import AgentRun, AgentSession
from app.agent_runtime.repository import utc_now
from app.pi_gateway.scheduler import GatewayModeError, PiRunScheduler, QueueTenant
from app.runtime_config.service import LEGACY_RUNTIME_CONFIG_ID
from app.licensing.models import TenantLicense
from app.tenancy.models import Tenant
from app.tenancy.models import TenantMembership


def test_fifo_order_is_oldest_queued_at_then_stable_id() -> None:
    tenants = {
        "tenant-a": QueueTenant(
            tenant_id="tenant-a",
            queued_count=1,
            oldest_queued_at=datetime(2026, 1, 1, 0, 0, 0),
            last_claimed_at=datetime(2026, 1, 1, 0, 0, 5),
        ),
        "tenant-b": QueueTenant(
            tenant_id="tenant-b",
            queued_count=1,
            oldest_queued_at=datetime(2026, 1, 1, 0, 0, 0),
            last_claimed_at=datetime(2026, 1, 1, 0, 0, 5),
        ),
    }
    assert PiRunScheduler.choose_fair_tenant(tenants) == "tenant-a"


@pytest.mark.asyncio
async def test_db_claim_is_single_owner_and_sets_session_slot(db_session, user_factory) -> None:
    user = await user_factory()
    tenant_id = await db_session.scalar(
        select(TenantMembership.tenant_id).where(TenantMembership.user_id == user.id)
    )
    now = utc_now()
    session = AgentSession(
        id=str(uuid4()),
        user_id=user.id,
        tenant_id=tenant_id,
        title="scheduler test",
        status="active",
        created_at=now,
        updated_at=now,
    )
    run = AgentRun(
        id=str(uuid4()),
        session_id=session.id,
        user_id=user.id,
        tenant_id=tenant_id,
        runtime_backend="pi",
        runtime_config_version_id=LEGACY_RUNTIME_CONFIG_ID,
        runtime_config_snapshot_json={"runtime_backend": "pi"},
        profile_name="session_analyst_v1",
        profile_version="v1",
        model="test-model",
        queued_at=now,
        created_at=now,
        status="queued",
        decision_count=0,
        review_count=0,
        revision_count=0,
    )
    db_session.add(session)
    await db_session.flush()
    db_session.add(run)
    await db_session.flush()

    scheduler = PiRunScheduler(db_session, now_fn=lambda: now)
    prepared = await scheduler.claim_next("gw-test", capacity=1)
    assert prepared is not None
    assert prepared.run.id == run.id
    assert prepared.attempt.run_id == run.id
    assert session.active_run_id == run.id
    assert await scheduler.claim_next("gw-other", capacity=1) is None


async def test_claim_rechecks_license_capacity_after_candidate_snapshot(
    db_session, user_factory
) -> None:
    user = await user_factory()
    tenant_id = await db_session.scalar(
        select(TenantMembership.tenant_id).where(TenantMembership.user_id == user.id)
    )
    tenant = await db_session.get(Tenant, tenant_id)
    assert tenant is not None and tenant.active_license_id is not None
    license_row = await db_session.get(TenantLicense, tenant.active_license_id)
    assert license_row is not None
    license_row.max_concurrent_runs = 1
    license_row.max_user_concurrent_runs = 1
    now = utc_now()
    session = AgentSession(
        id=str(uuid4()), user_id=user.id, tenant_id=tenant_id, title="stale candidate",
        status="active", created_at=now, updated_at=now,
    )
    queued = AgentRun(
        id=str(uuid4()), session_id=session.id, user_id=user.id, tenant_id=tenant_id,
        runtime_backend="pi", runtime_config_version_id=LEGACY_RUNTIME_CONFIG_ID,
        runtime_config_snapshot_json={"runtime_backend": "pi"}, profile_name="session_analyst_v1",
        profile_version="v1", model="test-model", queued_at=now, created_at=now,
        status="queued", decision_count=0, review_count=0, revision_count=0,
    )
    db_session.add(session)
    await db_session.flush()
    db_session.add(queued)
    await db_session.flush()
    scheduler = PiRunScheduler(db_session, now_fn=lambda: now)

    async def stale_candidates(_now):
        active_session = AgentSession(
            id=str(uuid4()), user_id=user.id, tenant_id=tenant_id, title="active",
            status="active", created_at=now, updated_at=now,
        )
        active = AgentRun(
            id=str(uuid4()), session_id=active_session.id, user_id=user.id, tenant_id=tenant_id,
            runtime_backend="pi", runtime_config_version_id=LEGACY_RUNTIME_CONFIG_ID,
            runtime_config_snapshot_json={"runtime_backend": "pi"}, profile_name="session_analyst_v1",
            profile_version="v1", model="test-model", queued_at=now, created_at=now,
            status="running", decision_count=0, review_count=0, revision_count=0,
        )
        db_session.add(active_session)
        await db_session.flush()
        db_session.add(active)
        await db_session.flush()
        return {tenant_id: QueueTenant(tenant_id, 1, now, None)}

    scheduler._candidate_tenants = stale_candidates
    assert await scheduler.claim_next("gw-capacity", capacity=1) is None
    await db_session.refresh(queued)
    assert queued.status == "queued"


async def test_claim_fails_closed_when_legacy_active_slot_was_not_backfilled(
    db_session, user_factory
) -> None:
    user = await user_factory()
    tenant_id = await db_session.scalar(
        select(TenantMembership.tenant_id).where(TenantMembership.user_id == user.id)
    )
    now = utc_now()
    session = AgentSession(
        id=str(uuid4()), user_id=user.id, tenant_id=tenant_id, title="legacy slot",
        status="active", created_at=now, updated_at=now,
    )
    active = AgentRun(
        id=str(uuid4()), session_id=session.id, user_id=user.id, tenant_id=tenant_id,
        runtime_backend="pi", runtime_config_version_id=LEGACY_RUNTIME_CONFIG_ID,
        runtime_config_snapshot_json={"runtime_backend": "pi"}, profile_name="session_analyst_v1",
        profile_version="v1", model="test-model", queued_at=now, created_at=now,
        status="running", decision_count=0, review_count=0, revision_count=0,
    )
    queued = AgentRun(
        id=str(uuid4()), session_id=session.id, user_id=user.id, tenant_id=tenant_id,
        runtime_backend="pi", runtime_config_version_id=LEGACY_RUNTIME_CONFIG_ID,
        runtime_config_snapshot_json={"runtime_backend": "pi"}, profile_name="session_analyst_v1",
        profile_version="v1", model="test-model", queued_at=now, created_at=now,
        status="queued", decision_count=0, review_count=0, revision_count=0,
    )
    db_session.add(session)
    await db_session.flush()
    db_session.add_all([active, queued])
    await db_session.flush()
    scheduler = PiRunScheduler(db_session, now_fn=lambda: now)

    with pytest.raises(GatewayModeError, match="pi_gateway_session_mutex_conflict"):
        await scheduler.claim_next("gw-legacy-slot", capacity=1)
