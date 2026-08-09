import pytest
from types import SimpleNamespace

from app.pi_gateway import router as gateway_router
from app.pi_gateway.contracts import PiGatewayTerminalRequest

from app.pi_gateway.internal_tools import ProductionInternalToolBridge


@pytest.mark.asyncio
async def test_internal_tool_bridge_does_not_accept_identity_from_body() -> None:
    bridge = ProductionInternalToolBridge(registry_factory=lambda: None)
    with pytest.raises(ValueError, match="pi_gateway_tool_context_required"):
        await bridge.execute(
            tool_name="get_session_context",
            arguments={"user_id": "other", "session_id": "other", "run_id": "other"},
            user_id="user-1",
            session_id="session-1",
            run_id="run-1",
            profile_name="pi_production",
        )
    with pytest.raises(ValueError, match="pi_gateway_tool_context_required"):
        await bridge.execute(
            tool_name="get_session_context",
            arguments={"filters": {"tenant_id": "other"}},
            user_id="user-1",
            session_id="session-1",
            run_id="run-1",
            profile_name="pi_production",
        )


@pytest.mark.asyncio
async def test_terminal_settles_with_gateway_worker_and_releases_session_slot(monkeypatch) -> None:
    run = SimpleNamespace(
        id="run-1",
        user_id="user-1",
        session_id="session-1",
        gateway_lease_hash="hash",
        gateway_lease_expires_at=object(),
        gateway_id="gw-1",
        lease_owner="gw-1",
        lease_expires_at=object(),
    )
    attempt = SimpleNamespace(id="attempt-1", run_id="run-1", outcome="running", ended_at=None)
    session = SimpleNamespace(id="session-1", active_run_id="run-1")

    async def authenticate(*_args, **_kwargs):
        return "gw-1"

    async def leased_run(*_args, **_kwargs):
        return run

    class FakeStream:
        def __init__(self, *_args, **_kwargs):
            self.worker_id = None

        async def settle_terminal(self, *_args, **kwargs):
            self.worker_id = kwargs.get("worker_id")
            callback = kwargs.get("before_commit")
            if callback is not None:
                await callback(run)
            return SimpleNamespace(id="event-1")

    class FakeDb:
        def __init__(self):
            self.calls = 0

        async def scalar(self, _statement):
            self.calls += 1
            return {1: "session-1", 2: session, 3: attempt}[self.calls]

        async def commit(self):
            return None

    monkeypatch.setattr(gateway_router, "_authenticate", authenticate)
    monkeypatch.setattr(
        gateway_router,
        "_service",
        lambda *_args, **_kwargs: SimpleNamespace(
            leased_run=leased_run,
            scheduler=SimpleNamespace(release_run=lambda *_args, **_kwargs: _noop()),
            now_fn=lambda: object(),
        ),
    )
    monkeypatch.setattr(gateway_router, "AgentEventStream", FakeStream)
    db = FakeDb()
    async def body() -> bytes:
        return b"{}"

    request = SimpleNamespace(
        body=body,
        app=SimpleNamespace(state=SimpleNamespace(agent_event_broker=object())),
    )
    result = await gateway_router.terminal(
        "run-1",
        request,
        PiGatewayTerminalRequest(attempt_id="attempt-1", outcome="completed"),
        db,
        "lease-token",
    )
    assert result == {"event_id": "event-1", "status": "completed"}
    assert attempt.outcome == "completed"
    assert session.active_run_id is None
    assert run.gateway_id is None and run.lease_owner is None


async def _noop() -> None:
    return None
