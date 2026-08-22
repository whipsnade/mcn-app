from types import SimpleNamespace

import pytest

from app.pi_gateway import router as gateway_router
from app.pi_gateway.contracts import PiGatewaySourceEventBatch


@pytest.mark.asyncio
async def test_event_batch_commits_before_broker_and_replay_does_not_republish(monkeypatch) -> None:
    order: list[str] = []

    class FakeBroker:
        def __init__(self) -> None:
            self.published: list[object] = []

        async def publish(self, event: object) -> None:
            order.append("publish")
            self.published.append(event)

    class FakeDb:
        def __init__(self, event: object) -> None:
            self.event = event
            self.commits = 0

        async def commit(self) -> None:
            order.append("commit")
            self.commits += 1

        async def rollback(self) -> None:
            order.append("rollback")

        async def scalars(self, _statement):
            return SimpleNamespace(all=lambda: [self.event])

    class FakeService:
        def __init__(self) -> None:
            self.calls = 0

        async def ingest_source_event_batch(self, *_args, **_kwargs):
            order.append("ingest")
            self.calls += 1
            duplicate = self.calls > 1
            return {
                "receipts": [{
                    "source_event_id": "attempt-router:1",
                    "sequence": 1,
                    "duplicate": duplicate,
                    **({} if duplicate else {"event_id": "event-1"}),
                }],
                "last_acked_source_sequence": 1,
            }

    broker = FakeBroker()
    service = FakeService()
    db = FakeDb(SimpleNamespace(id="event-1"))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(agent_event_broker=broker)))
    payload = PiGatewaySourceEventBatch.model_validate({
        "events": [{
            "source_event_id": "attempt-router:1",
            "sequence": 1,
            "event_type": "message.start",
            "payload": {},
        }],
    })

    async def authenticate_run_access(*_args, **_kwargs) -> str:
        return "gateway-test"

    async def leased_run(*_args, **_kwargs) -> object:
        return SimpleNamespace(id="run-router")

    monkeypatch.setattr(gateway_router, "_authenticate_run_access", authenticate_run_access)
    monkeypatch.setattr(gateway_router, "_leased_run", leased_run)
    monkeypatch.setattr(gateway_router, "_service", lambda *_args, **_kwargs: service)

    first = await gateway_router.source_event_batch("run-router", request, payload, db, "lease-token")
    second = await gateway_router.source_event_batch("run-router", request, payload, db, "lease-token")

    assert first["receipts"][0]["event_id"] == "event-1"
    assert second["receipts"][0]["duplicate"] is True
    assert db.commits == 2
    assert broker.published == [db.event]
    assert order == ["ingest", "commit", "publish", "ingest", "commit"]
