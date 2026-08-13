from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.agent_runtime.models import AgentRun, AgentRunAttempt, AgentSession
from app.billing.models import RuntimeUsageRecord
from app.pi_gateway.accounting import RuntimeUsageError, RuntimeUsageService
from app.tenancy.models import TenantMembership


def _snapshot(price_table: dict[str, int | str] | None = None) -> dict:
    billing: dict[str, object] = {}
    if price_table is not None:
        billing["price_table"] = price_table
    return {
        "config_version_id": "tenant-pi-price-v1",
        "runtime_contract_version": "marketing_runtime_v1",
        "runtime_backend": "pi",
        "model": {"provider": "fake-provider", "name": "fake-model"},
        "datatap": {"service": "fake", "schema_digest": "sha256:" + "a" * 64},
        "capability_pack": {
            "runtime_contract_version": "marketing_runtime_v1",
            "pack_version": "test-pack-v1",
            "manifest_digest": "test-manifest-digest",
        },
        "profile_name": "utility_v1",
        "artifact_contract_mode": "none",
        "capability_pack_version": "test-pack-v1",
        "capability_pack_manifest_digest": "test-manifest-digest",
        "limits": {"max_decisions": 50},
        "billing": billing,
    }


async def _run(db_session, user, *, snapshot: dict | None = None):
    membership = await db_session.scalar(
        select(TenantMembership).where(TenantMembership.user_id == user.id)
    )
    assert membership is not None
    now = datetime.now(UTC).replace(tzinfo=None)
    session = AgentSession(
        id=f"session-usage-{user.id[:8]}",
        user_id=user.id,
        tenant_id=membership.tenant_id,
        title="usage test",
        status="active",
        created_at=now,
        updated_at=now,
    )
    run = AgentRun(
        id=f"run-usage-{user.id[:8]}",
        session_id=session.id,
        user_id=user.id,
        tenant_id=membership.tenant_id,
        runtime_backend="pi",
        runtime_config_version_id=None,
        runtime_config_snapshot_json=None,
        queued_at=None,
        profile_name="brand_analysis",
        profile_version="1",
        model="fake-model",
        status="running",
        created_at=now,
    )
    attempt = AgentRunAttempt(
        id=f"attempt-usage-{user.id[:8]}",
        run_id=run.id,
        attempt=1,
        started_at=now,
        outcome="running",
    )
    db_session.add(session)
    await db_session.flush()
    db_session.add_all([run, attempt])
    await db_session.flush()
    snapshot_payload = dict(_snapshot())
    snapshot_payload.update(snapshot or {})
    if snapshot and "artifact_contract_mode" not in snapshot:
        snapshot_payload["artifact_contract_mode"] = (
            "required" if snapshot_payload.get("required_artifact_contract") else "none"
        )
    run.runtime_config_snapshot_json = snapshot_payload
    await db_session.flush()
    return run, attempt, membership.tenant_id


@pytest.mark.asyncio
async def test_model_usage_is_priced_from_run_snapshot_and_duplicate_is_idempotent(
    db_session, user_factory
) -> None:
    user = await user_factory()
    run, attempt, _tenant_id = await _run(
        db_session,
        user,
        snapshot=_snapshot(
            {
                "input_micros_per_million": 2_000_000,
                "output_micros_per_million": 4_000_000,
                "cache_read_micros_per_million": 500_000,
                "cache_write_micros_per_million": 1_000_000,
                "currency": "USD",
            }
        ),
    )
    service = RuntimeUsageService(db_session)
    payload = {
        "input_tokens": 2_000,
        "output_tokens": 1_000,
        "cache_read_tokens": 500,
        "cache_write_tokens": 100,
        "upstream_request_id": "provider-request-1",
        "provider": "model-must-come-from-snapshot",
        "model": "model-must-come-from-snapshot",
    }

    first = await service.record_model_usage(run, attempt.id, f"{attempt.id}:1", payload)
    duplicate = await service.record_model_usage(run, attempt.id, f"{attempt.id}:1", payload)

    assert duplicate.id == first.id
    assert first.provider == "fake-provider"
    assert first.model == "fake-model"
    assert first.usage_status == "available"
    assert first.cost_status == "priced"
    assert first.cost_micros == 8_350
    assert first.currency == "USD"
    assert len((await db_session.scalars(select(RuntimeUsageRecord))).all()) == 1


@pytest.mark.asyncio
async def test_missing_usage_is_unavailable_and_never_estimated_as_zero(db_session, user_factory) -> None:
    user = await user_factory()
    run, attempt, _tenant_id = await _run(db_session, user, snapshot=_snapshot())
    record = await RuntimeUsageService(db_session).record_model_usage(
        run,
        attempt.id,
        f"{attempt.id}:1",
        {"upstream_request_id": "request-without-usage"},
    )

    assert record.usage_status == "unavailable"
    assert record.cost_micros is None
    assert record.input_tokens is None
    assert record.output_tokens is None
    assert record.cost_status == "unavailable"


@pytest.mark.asyncio
async def test_usage_price_is_snapshot_versioned_and_unpriced_without_rates(db_session, user_factory) -> None:
    user = await user_factory()
    run, attempt, _tenant_id = await _run(
        db_session,
        user,
        snapshot=_snapshot({"input_micros_per_million": 1_000_000, "currency": "USD"}),
    )
    service = RuntimeUsageService(db_session)
    priced = await service.record_model_usage(
        run, attempt.id, f"{attempt.id}:1", {"input_tokens": 3}
    )
    assert priced.cost_micros == 3
    assert priced.cost_status == "priced"

    run.runtime_config_snapshot_json = _snapshot(
        {"output_micros_per_million": 2_000_000, "currency": "EUR"}
    )
    unpriced = await service.record_model_usage(
        run, attempt.id, f"{attempt.id}:2", {"input_tokens": 3}
    )
    assert unpriced.cost_micros is None
    assert unpriced.cost_status == "unpriced"
    assert unpriced.currency == "EUR"


@pytest.mark.asyncio
async def test_usage_rejects_cross_attempt_and_conflicting_replay(db_session, user_factory) -> None:
    user = await user_factory()
    run, attempt, _tenant_id = await _run(db_session, user)
    other_attempt = AgentRunAttempt(
        id=f"attempt-usage-other-{user.id[:8]}",
        run_id=run.id,
        attempt=2,
        started_at=attempt.started_at,
        outcome="running",
    )
    db_session.add(other_attempt)
    await db_session.flush()
    service = RuntimeUsageService(db_session)
    await service.record_model_usage(run, attempt.id, f"{attempt.id}:1", {"input_tokens": 1})

    with pytest.raises(RuntimeUsageError, match="runtime_usage_attempt_mismatch"):
        await service.record_model_usage(run, other_attempt.id, f"{attempt.id}:2", {"input_tokens": 1})
    with pytest.raises(RuntimeUsageError, match="runtime_usage_idempotency_conflict"):
        await service.record_model_usage(run, attempt.id, f"{attempt.id}:1", {"input_tokens": 2})


@pytest.mark.asyncio
async def test_current_backend_model_usage_uses_the_same_cost_projection(db_session, user_factory) -> None:
    user = await user_factory()
    run, attempt, _tenant_id = await _run(
        db_session,
        user,
        snapshot=_snapshot({"input_micros_per_million": 1_000_000}),
    )
    run.runtime_backend = "current"
    record = await RuntimeUsageService(db_session).record_model_usage(
        run,
        attempt.id,
        f"{attempt.id}:1",
        {"input_tokens": 2},
    )
    assert record.backend == "current"
    assert record.input_tokens == 2
    assert record.cost_micros == 2
