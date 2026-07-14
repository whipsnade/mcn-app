import pytest

from app.model.contracts import ModelAdapterError, StructuredResult
from app.model.fake import FakeModelAdapter
from .test_tencent_plan import MinimalPlan, planner_request


@pytest.mark.asyncio
async def test_fake_adapter_returns_scripted_results_and_records_requests() -> None:
    expected = StructuredResult[MinimalPlan](
        value=MinimalPlan(objective="选人", steps=[]),
        usage=None,
        request_id=None,
        regeneration_count=0,
    )
    adapter = FakeModelAdapter(structured_results=[expected])

    result = await adapter.complete_json(planner_request())

    assert result == expected
    assert adapter.structured_requests == [planner_request()]


@pytest.mark.asyncio
async def test_fake_adapter_default_never_attempts_network() -> None:
    adapter = FakeModelAdapter()

    with pytest.raises(ModelAdapterError) as caught:
        await adapter.complete_json(planner_request())

    assert caught.value.code == "FAKE_MODEL_NOT_SCRIPTED"
