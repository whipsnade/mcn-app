"""Production lifespan wiring for the Pi recovery loop (separate from current)."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_create_agent_runtime_wires_a_pi_scoped_recovery_loop() -> None:
    from app.main import create_agent_runtime

    _executor, _recovery, _broker, _engine_factory, _utility, pi_recovery = create_agent_runtime()
    assert pi_recovery._runtime_backend == "pi"  # noqa: SLF001
    assert pi_recovery._pi_recovery is not None  # noqa: SLF001
    assert pi_recovery._pi_cancel is not None  # noqa: SLF001


@pytest.mark.asyncio
async def test_lifespan_starts_and_stops_both_recovery_loops(
    monkeypatch, tmp_path
) -> None:
    from app.main import create_app

    monkeypatch.setenv("AGENT_UPLOAD_STORAGE_DIR", str(tmp_path / "uploads"))
    app = create_app()
    async with app.router.lifespan_context(app):
        assert app.state.agent_recovery._loop_task is not None  # noqa: SLF001
        assert app.state.agent_pi_recovery._loop_task is not None  # noqa: SLF001
    assert app.state.agent_recovery._loop_task is None  # noqa: SLF001
    assert app.state.agent_pi_recovery._loop_task is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_repeated_lifespan_rebuilds_cached_transports(
    monkeypatch, tmp_path
) -> None:
    """Closing one app lifespan must not leave the global transport cache poisoned."""
    from app import main as app_main

    async def no_refresh() -> None:
        return None

    monkeypatch.setattr(app_main, "refresh_approved_datatap_tools", no_refresh)
    monkeypatch.setenv("AGENT_UPLOAD_STORAGE_DIR", str(tmp_path / "uploads"))
    app_main.get_agent_mcp_transport.cache_clear()
    app_main.get_mcp_transport.cache_clear()
    try:
        first = app_main.create_app()
        async with first.router.lifespan_context(first):
            first_agent = app_main.get_agent_mcp_transport()
            first_legacy = app_main.get_mcp_transport()
        assert first_agent._client.is_closed  # noqa: SLF001
        assert first_legacy._client.is_closed  # noqa: SLF001

        second = app_main.create_app()
        async with second.router.lifespan_context(second):
            second_agent = app_main.get_agent_mcp_transport()
            second_legacy = app_main.get_mcp_transport()
            assert second_agent is not first_agent
            assert second_legacy is not first_legacy
            assert not second_agent._client.is_closed  # noqa: SLF001
            assert not second_legacy._client.is_closed  # noqa: SLF001
    finally:
        app_main.get_agent_mcp_transport.cache_clear()
        app_main.get_mcp_transport.cache_clear()


@pytest.mark.asyncio
async def test_wired_pi_recovery_callback_is_the_real_service(db_session, user_factory) -> None:
    """The wired callback must construct and drive PiGatewayRecoveryService.

    A run id that does not exist yields the service's ``ignored`` outcome —
    proving the callback reaches the database through the real service rather
    than a stub.  Behavioural recovery coverage lives in test_recovery.py and
    the process-level offline UAT.
    """
    from app.main import create_agent_runtime

    *_, pi_recovery = create_agent_runtime()
    outcome = await pi_recovery._pi_recovery("run-that-does-not-exist")  # noqa: SLF001
    assert outcome == "ignored"
    assert await pi_recovery._pi_cancel("run-that-does-not-exist") is False  # noqa: SLF001
