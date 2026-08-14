import pytest
from sqlalchemy import select

from app.agent_runtime.models import AgentEvent
from app.billing.models import RuntimeUsageRecord
from app.pi_gateway.events import PiGatewayEventError
from app.pi_gateway.service import PiGatewayService

from .test_model_usage import _run


@pytest.mark.asyncio
async def test_usage_and_visible_events_share_one_attempt_sequence(db_session, user_factory) -> None:
    user = await user_factory()
    run, attempt, _tenant_id = await _run(db_session, user)
    service = PiGatewayService(db_session, gateway_id="gateway-test")
    usage = await service.ingest_source_event(
        run,
        attempt_id=attempt.id,
        source_event_id=f"{attempt.id}:1",
        sequence=1,
        event_type="usage",
        payload={"input_tokens": 2},
    )
    visible = await service.ingest_source_event(
        run,
        attempt_id=attempt.id,
        source_event_id=f"{attempt.id}:2",
        sequence=2,
        event_type="tool.start",
        payload={"call_id": "call-1", "internal_tool_name": "load_marketing_skill"},
    )
    await db_session.commit()
    assert usage["sequence"] == 1
    assert visible["duplicate"] is False
    assert len(
        list((await db_session.scalars(select(RuntimeUsageRecord).where(RuntimeUsageRecord.run_id == run.id))).all())
    ) == 1
    event = await db_session.scalar(select(AgentEvent).where(AgentEvent.run_id == run.id))
    assert event is not None
    assert event.event_type == "tool.started"

    with pytest.raises(PiGatewayEventError, match="pi_gateway_source_sequence_gap"):
        await service.ingest_source_event(
            run,
            attempt_id=attempt.id,
            source_event_id=f"{attempt.id}:4",
            sequence=4,
            event_type="tool.end",
            payload={"call_id": "call-1", "status": "succeeded"},
        )
