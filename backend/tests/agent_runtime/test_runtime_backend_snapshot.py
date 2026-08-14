from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.agent_runtime.models import AgentRun, AgentSession
from app.runtime_config.service import RuntimeConfigService
from app.runtime_config.crypto import RuntimeConfigError
from app.tenancy.models import Tenant


@pytest.mark.asyncio
async def test_new_run_snapshot_is_persisted_with_backend_and_config_version(
    db_session, user_factory
) -> None:
    user = await user_factory()
    tenant = await db_session.scalar(select(Tenant).where(Tenant.id.is_not(None)))
    snapshot = await RuntimeConfigService(db_session).snapshot_for_new_run(tenant.id)
    now = datetime.now(UTC).replace(tzinfo=None)
    session = AgentSession(
        id=str(uuid4()),
        user_id=user.id,
        tenant_id=tenant.id,
        title="snapshot",
        status="active",
        created_at=now,
        updated_at=now,
    )
    db_session.add(session)
    await db_session.flush()
    run = AgentRun(
        id=str(uuid4()),
        session_id=session.id,
        user_id=user.id,
        tenant_id=tenant.id,
        runtime_backend=snapshot.runtime_backend,
        runtime_config_version_id=snapshot.config_version_id,
        runtime_config_snapshot_json=snapshot.model_dump(mode="json"),
        queued_at=now,
        run_kind="user",
        visibility="user",
        profile_name="session_analyst_v1",
        profile_version="v1",
        model="test-model",
        status="queued",
        started_at=None,
    )
    db_session.add(run)
    await db_session.flush()
    assert run.runtime_backend == "current"
    assert run.runtime_config_version_id == snapshot.config_version_id
    assert run.runtime_config_snapshot_json["runtime_contract_version"] == "marketing_runtime_v1"

    run.runtime_config_snapshot_json = {
        **run.runtime_config_snapshot_json,
        "runtime_contract_version": "marketing_runtime_v0",
    }
    with pytest.raises(RuntimeConfigError, match="runtime_snapshot_invalid"):
        await RuntimeConfigService(db_session).snapshot_for_existing_run(run)
