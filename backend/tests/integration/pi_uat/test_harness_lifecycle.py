from __future__ import annotations

import asyncio
import socket

import pytest

from . import harness
from .harness import PiUatTopology, _InProcessServer


class _NeverStartedUvicorn:
    """started 永远为 False、serve() 长期等待的 fake uvicorn server。"""

    def __init__(self) -> None:
        self.should_exit = False
        self.started = False

    async def serve(self) -> None:
        await asyncio.Event().wait()


def _patch_fast_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 harness 的轮询 sleep 缩到 0，避免测试真实等待启动超时窗口。"""
    real_sleep = asyncio.sleep

    async def fast_sleep(_delay: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr(harness.asyncio, "sleep", fast_sleep)


def _assert_no_leftover_tasks(tasks_before: set[asyncio.Task[object]]) -> None:
    leftovers = [
        task for task in asyncio.all_tasks() if task not in tasks_before and not task.done()
    ]
    assert not leftovers, f"leftover tasks: {leftovers!r}"


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
async def test_in_process_start_timeout_cancels_and_reaps_serve_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fast_poll(monkeypatch)
    server = _InProcessServer(object(), 0)
    fake = _NeverStartedUvicorn()
    server._server = fake
    tasks_before = set(asyncio.all_tasks())

    with pytest.raises(RuntimeError, match="in_process_server_start_timeout"):
        await server.start()

    task = server._task
    assert task is not None
    assert task.done()
    assert task.cancelled()
    assert fake.should_exit
    assert server not in _InProcessServer._active_servers
    _assert_no_leftover_tasks(tasks_before)


@pytest.mark.asyncio
async def test_topology_reaps_in_process_server_when_start_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fast_poll(monkeypatch)
    made: list[_InProcessServer] = []

    def failing_server_factory(_app: object, port: int) -> _InProcessServer:
        server = _InProcessServer(object(), port)
        server._server = _NeverStartedUvicorn()
        made.append(server)
        return server

    monkeypatch.setattr(harness, "_InProcessServer", failing_server_factory)
    topology = PiUatTopology()
    teardown_calls: list[bool] = []

    async def record_teardown() -> None:
        teardown_calls.append(True)

    monkeypatch.setattr(topology, "_teardown_db", record_teardown)
    tasks_before = set(asyncio.all_tasks())

    with pytest.raises(RuntimeError, match="in_process_server_start_timeout"):
        await topology.__aenter__()

    # 外层 cleanup 仍完成，已创建的 serve task 被完整收口
    assert made, "fake server factory should have been used"
    for server in made:
        task = server._task
        assert task is not None
        assert task.done()
        assert server not in _InProcessServer._active_servers
    # 数据库 teardown 仍执行，原始启动异常不被静默吞掉（上面的 match 即原始错误）
    assert teardown_calls
    # 无监听端口残留：能重新 bind 说明 fake server 没有占用该端口
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", topology.model_port))
    finally:
        probe.close()
    _assert_no_leftover_tasks(tasks_before)


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
