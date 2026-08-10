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
