from __future__ import annotations

import asyncio

import pytest

from . import harness
from .harness import PiUatTopology, _InProcessServer


@pytest.mark.asyncio
async def test_in_process_stop_cancels_and_reaps_after_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _InProcessServer(object(), 0)
    task = asyncio.create_task(asyncio.sleep(3600))
    server._task = task

    async def timeout(_awaitable, *, timeout):
        del timeout
        raise asyncio.TimeoutError

    monkeypatch.setattr("tests.integration.pi_uat.harness.asyncio.wait_for", timeout)
    await server.stop()

    assert task.done()
    assert task.cancelled()


@pytest.mark.asyncio
async def test_topology_cleanup_finishes_when_outer_task_is_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    topology = PiUatTopology()
    started = asyncio.Event()
    finished = asyncio.Event()

    async def cleanup() -> None:
        started.set()
        await asyncio.sleep(0.05)
        finished.set()

    monkeypatch.setattr(topology, "_cleanup_impl", cleanup)
    exiting = asyncio.create_task(topology.__aexit__(None, None, None))
    await started.wait()
    exiting.cancel()

    with pytest.raises(asyncio.CancelledError):
        await exiting
    assert finished.is_set()


@pytest.mark.asyncio
async def test_topology_startup_base_exception_still_runs_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    topology = PiUatTopology()
    cleaned = asyncio.Event()

    async def start() -> None:
        raise KeyboardInterrupt

    async def cleanup() -> None:
        cleaned.set()

    monkeypatch.setattr(topology, "_start_topology", start)
    monkeypatch.setattr(topology, "_cleanup_impl", cleanup)

    with pytest.raises(KeyboardInterrupt):
        await topology.__aenter__()
    assert cleaned.is_set()


def test_each_topology_has_a_unique_gateway_id() -> None:
    first = PiUatTopology()
    second = PiUatTopology()

    assert first.gateway_id != second.gateway_id
    assert first.gateway_id.startswith("gw-uat-")
    assert second.gateway_id.startswith("gw-uat-")


def test_destructive_uat_scope_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("MYSQL_DATABASE", "kol_insight")
    monkeypatch.setenv("MYSQL_USER", "developer")

    with pytest.raises(RuntimeError, match="uat_database_scope_invalid"):
        harness.assert_uat_database_scope()
