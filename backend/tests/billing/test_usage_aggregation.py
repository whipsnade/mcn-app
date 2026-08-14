import pytest
from sqlalchemy import select

from app.billing.models import RuntimeUsageRecord
from app.pi_gateway.accounting import RuntimeUsageService
from tests.pi_gateway.test_model_usage import _run


@pytest.mark.asyncio
async def test_usage_aggregation_is_tenant_scoped_and_integer_micros(db_session, user_factory) -> None:
    first_user = await user_factory()
    first_run, first_attempt, first_tenant = await _run(
        db_session,
        first_user,
        snapshot={
            "model": {"provider": "fake-provider", "name": "fake-model"},
            "billing": {
                "price_table": {
                    "input_micros_per_million": 1_000_000,
                    "output_micros_per_million": 2_000_000,
                    "currency": "USD",
                }
            },
        },
    )
    other_user = await user_factory()
    other_run, other_attempt, other_tenant = await _run(db_session, other_user)
    service = RuntimeUsageService(db_session)
    await service.record_model_usage(
        first_run, first_attempt.id, f"{first_attempt.id}:1", {"input_tokens": 1_500, "output_tokens": 2}
    )
    await service.record_model_usage(
        first_run, first_attempt.id, f"{first_attempt.id}:2", {"input_tokens": 500, "output_tokens": 3}
    )
    await service.record_model_usage(
        other_run, other_attempt.id, f"{other_attempt.id}:1", {"input_tokens": 999, "output_tokens": 999}
    )

    aggregates = await service.aggregate_usage(first_tenant, group_by="day")

    assert len(aggregates) == 1
    assert aggregates[0].input_tokens == 2_000
    assert aggregates[0].output_tokens == 5
    assert aggregates[0].cost_micros == 2_010
    assert aggregates[0].tenant_id == first_tenant
    assert all(row.tenant_id != first_tenant for row in (await db_session.scalars(
        select(RuntimeUsageRecord).where(RuntimeUsageRecord.tenant_id == other_tenant)
    )).all())


@pytest.mark.asyncio
async def test_usage_aggregation_can_filter_user_and_run_without_float_costs(db_session, user_factory) -> None:
    user = await user_factory()
    run, attempt, tenant_id = await _run(db_session, user)
    service = RuntimeUsageService(db_session)
    await service.record_model_usage(run, attempt.id, f"{attempt.id}:1", {"input_tokens": 1})
    rows = await service.aggregate_usage(tenant_id, user_id=user.id, run_id=run.id, group_by="run")
    assert len(rows) == 1
    assert rows[0].run_id == run.id
    assert isinstance(rows[0].cost_micros, (int, type(None)))
