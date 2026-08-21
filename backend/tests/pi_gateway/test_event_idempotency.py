import pytest
from sqlalchemy import select

from app.agent_runtime.models import AgentEvent, AgentMessage
from app.billing.models import RuntimeUsageRecord
from app.pi_gateway.events import PiGatewayEventError
from app.pi_gateway.service import PiGatewayService

from .test_model_usage import _run


@pytest.mark.asyncio
async def test_gateway_events_are_idempotent_and_merge_one_assistant_message(db_session, user_factory) -> None:
    user = await user_factory()
    run, attempt, _tenant_id = await _run(db_session, user)
    service = PiGatewayService(db_session, gateway_id="gateway-test")

    first = await service.ingest_source_event(
        run,
        attempt_id=attempt.id,
        source_event_id=f"{attempt.id}:1",
        sequence=1,
        event_type="text.delta",
        payload={"delta": "hello "},
    )
    duplicate = await service.ingest_source_event(
        run,
        attempt_id=attempt.id,
        source_event_id=f"{attempt.id}:1",
        sequence=1,
        event_type="text.delta",
        payload={"delta": "tampered"},
    )
    second = await service.ingest_source_event(
        run,
        attempt_id=attempt.id,
        source_event_id=f"{attempt.id}:2",
        sequence=2,
        event_type="message.end",
        payload={},
    )
    await db_session.commit()

    assert first["duplicate"] is False
    assert duplicate["duplicate"] is True
    assert second["duplicate"] is False
    events = list((await db_session.scalars(select(AgentEvent).where(AgentEvent.run_id == run.id))).all())
    assert [event.event_type for event in events] == ["message.delta", "message.completed"]
    messages = list(
        (
            await db_session.scalars(
                select(AgentMessage).where(AgentMessage.run_id == run.id, AgentMessage.role == "assistant")
            )
        ).all()
    )
    assert len(messages) == 1
    assert messages[0].content == "hello "


@pytest.mark.asyncio
async def test_gateway_events_reject_gap_cross_attempt_and_second_completion(db_session, user_factory) -> None:
    user = await user_factory()
    run, attempt, _tenant_id = await _run(db_session, user)
    service = PiGatewayService(db_session, gateway_id="gateway-test")

    with pytest.raises(PiGatewayEventError, match="pi_gateway_source_sequence_gap"):
        await service.ingest_source_event(
            run,
            attempt_id=attempt.id,
            source_event_id=f"{attempt.id}:2",
            sequence=2,
            event_type="message.end",
            payload={},
        )
    with pytest.raises(PiGatewayEventError, match="pi_gateway_source_event_attempt_mismatch"):
        await service.ingest_source_event(
            run,
            attempt_id=attempt.id,
            source_event_id="other-attempt:1",
            sequence=1,
            event_type="message.end",
            payload={},
        )


@pytest.mark.asyncio
async def test_gateway_event_batch_is_atomic_and_replay_returns_original_receipts(db_session, user_factory) -> None:
    user = await user_factory()
    run, attempt, _tenant_id = await _run(db_session, user)
    service = PiGatewayService(db_session, gateway_id="gateway-test")
    events = [
        {
            "source_event_id": f"{attempt.id}:1",
            "sequence": 1,
            "event_type": "usage",
            "payload": {"input_tokens": 2, "output_tokens": 1},
        },
        {
            "source_event_id": f"{attempt.id}:2",
            "sequence": 2,
            "event_type": "text.delta",
            "payload": {"delta": "hello "},
        },
        {
            "source_event_id": f"{attempt.id}:3",
            "sequence": 3,
            "event_type": "message.end",
            "payload": {},
        },
    ]

    first = await service.ingest_source_event_batch(
        run,
        attempt_id=attempt.id,
        events=events,
    )
    await db_session.commit()
    replay = await service.ingest_source_event_batch(
        run,
        attempt_id=attempt.id,
        events=events,
    )
    await db_session.commit()

    assert replay == first
    assert [item["sequence"] for item in first["receipts"]] == [1, 2, 3]
    assert all(item["duplicate"] is False for item in first["receipts"])
    assert len(list((await db_session.scalars(select(AgentEvent).where(AgentEvent.run_id == run.id))).all())) == 2
    assert len(
        list((await db_session.scalars(select(RuntimeUsageRecord).where(RuntimeUsageRecord.run_id == run.id))).all())
    ) == 1
    message = await db_session.scalar(
        select(AgentMessage).where(AgentMessage.run_id == run.id, AgentMessage.role == "assistant")
    )
    assert message is not None
    assert message.content == "hello "


@pytest.mark.asyncio
async def test_gateway_event_batch_rejects_gap_without_partial_write(db_session, user_factory) -> None:
    user = await user_factory()
    run, attempt, _tenant_id = await _run(db_session, user)
    service = PiGatewayService(db_session, gateway_id="gateway-test")

    with pytest.raises(PiGatewayEventError, match="pi_gateway_source_sequence_gap"):
        await service.ingest_source_event_batch(
            run,
            attempt_id=attempt.id,
            events=[
                {
                    "source_event_id": f"{attempt.id}:1",
                    "sequence": 1,
                    "event_type": "text.delta",
                    "payload": {"delta": "first"},
                },
                {
                    "source_event_id": f"{attempt.id}:3",
                    "sequence": 3,
                    "event_type": "message.end",
                    "payload": {},
                },
            ],
        )
    assert not await db_session.scalar(select(AgentEvent).where(AgentEvent.run_id == run.id))
