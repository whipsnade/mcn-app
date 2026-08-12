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
async def test_in_process_stop_fails_bounded_when_task_ignores_cancel() -> None:
    server = _InProcessServer(object(), 0)
    release = asyncio.Event()

    async def stubborn() -> None:
        while not release.is_set():
            try:
                await asyncio.wait_for(release.wait(), timeout=3600)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                continue

    task = asyncio.create_task(stubborn())
    server._task = task

    with pytest.raises(RuntimeError, match="in_process_server_stop_timeout"):
        await server.stop()
    release.set()
    task.cancel()
    await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_stop_processes_kills_recorded_group_after_parent_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    topology = PiUatTopology()
    proc = type("Proc", (), {"pid": 1234, "poll": lambda _self: 1})()
    topology._gateway = proc
    topology._gateway_pgid = 1234
    calls: list[tuple[int, int]] = []

    monkeypatch.setattr(harness.os, "killpg", lambda pgid, sig: calls.append((pgid, sig)))
    monkeypatch.setattr(harness, "_wait_pgid_gone", lambda _pgid, _timeout: _gone())

    async def _gone() -> bool:
        return True

    await topology._stop_processes()
    assert calls == [(1234, harness.signal.SIGTERM)]


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


@pytest.mark.asyncio
async def test_destructive_scope_checks_actual_database_and_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("MYSQL_DATABASE", "kol_insight_test")
    monkeypatch.setenv("MYSQL_USER", "kol_test")

    class Result:
        def one(self):
            return "kol_insight", "kol_test@localhost"

    class Db:
        async def execute(self, _statement):
            return Result()

    with pytest.raises(RuntimeError, match="uat_database_connection_scope_invalid"):
        await harness.assert_uat_database_connection(Db())
