import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from app.pi_runtime_poc.rpc import (
    PiRpcClient,
    PiRpcConfig,
    PiRpcExited,
    PiRpcProtocolError,
    PiRpcTimeout,
)


class FakeReader:
    """只实现 read，确保客户端不依赖 readline 的缓冲语义。"""

    def __init__(self) -> None:
        self._chunks: asyncio.Queue[bytes | None] = asyncio.Queue()

    def feed(self, value: bytes) -> None:
        self._chunks.put_nowait(value)

    def finish(self) -> None:
        self._chunks.put_nowait(None)

    async def read(self, _size: int = -1) -> bytes:
        value = await self._chunks.get()
        return b"" if value is None else value


class FakeStdin:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, value: bytes) -> None:
        self.writes.append(value)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class FakeProcess:
    def __init__(self) -> None:
        self.stdin = FakeStdin()
        self.stdout = FakeReader()
        self.stderr = FakeReader()
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self._exited = asyncio.Event()

    async def wait(self) -> int:
        await self._exited.wait()
        assert self.returncode is not None
        return self.returncode

    def exit(self, code: int) -> None:
        self.returncode = code
        self.stdout.finish()
        self.stderr.finish()
        self._exited.set()

    def terminate(self) -> None:
        self.terminated = True
        self.exit(-15)

    def kill(self) -> None:
        self.killed = True
        self.exit(-9)


@pytest.fixture
def fake_process(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeProcess, list[tuple[Any, ...]], list[dict[str, Any]]]:
    process = FakeProcess()
    calls: list[tuple[Any, ...]] = []
    kwargs_calls: list[dict[str, Any]] = []

    async def spawn(*args: Any, **kwargs: Any) -> FakeProcess:
        calls.append(args)
        kwargs_calls.append(kwargs)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    return process, calls, kwargs_calls


def _config(**changes: Any) -> PiRpcConfig:
    values = {
        "executable": "/opt/pi/bin/pi",
        "extensions": ("/trusted/extensions/datap.mjs", "/trusted/extensions/events.mjs"),
        "skills": ("/trusted/skills/brand/SKILL.md",),
        "timeout_seconds": 30 * 60,
    }
    values.update(changes)
    return PiRpcConfig(**values)


async def _next_event(client: PiRpcClient) -> dict[str, Any]:
    return await anext(client.events())


async def test_prompt_uses_correlation_id_and_strict_isolated_command(
    fake_process: tuple[FakeProcess, list[tuple[Any, ...]], list[dict[str, Any]]],
) -> None:
    process, calls, kwargs_calls = fake_process
    client = await PiRpcClient.start(_config())
    message = "test"

    request_id = await client.prompt(message)

    assert calls == [
        (
            "/opt/pi/bin/pi",
            "--mode",
            "rpc",
            "--no-session",
            "--no-builtin-tools",
            "--no-context-files",
            "--no-extensions",
            "-e",
            "/trusted/extensions/datap.mjs",
            "-e",
            "/trusted/extensions/events.mjs",
            "--no-skills",
            "--skill",
            "/trusted/skills/brand/SKILL.md",
        )
    ]
    assert json.loads(process.stdin.writes[0]) == {
        "id": request_id,
        "type": "prompt",
        "message": message,
    }
    assert process.stdin.writes[0].endswith(b"\n")
    assert kwargs_calls[0]["env"]["PI_OFFLINE"] == "1"
    assert kwargs_calls[0]["env"]["PI_SKIP_VERSION_CHECK"] == "1"
    agent_dir = Path(kwargs_calls[0]["env"]["PI_CODING_AGENT_DIR"])
    assert agent_dir.is_dir()

    process.stdout.feed(
        json.dumps(
            {"id": request_id, "type": "response", "command": "prompt", "success": True}
        ).encode()
        + b"\n"
    )
    assert (await _next_event(client))["id"] == request_id

    await client.close()
    assert not agent_dir.exists()


async def test_events_frame_split_crlf_and_literal_u2028_without_readline(
    fake_process: tuple[FakeProcess, list[tuple[Any, ...]], list[dict[str, Any]]],
) -> None:
    process, _, _ = fake_process
    client = await PiRpcClient.start(_config())
    record = '{"type":"message","text":"甲\u2028乙"}'

    process.stdout.feed(record[:14].encode("utf-8"))
    process.stdout.feed(record[14:].encode("utf-8") + b"\r")
    process.stdout.feed(b"\n")

    assert await _next_event(client) == {"type": "message", "text": "甲\u2028乙"}
    await client.close()


async def test_process_exit_exposes_stderr_without_treating_it_as_rpc(
    fake_process: tuple[FakeProcess, list[tuple[Any, ...]], list[dict[str, Any]]],
) -> None:
    process, _, _ = fake_process
    client = await PiRpcClient.start(_config())

    process.stderr.feed(b"pi diagnostic only\n")
    process.exit(17)

    with pytest.raises(PiRpcExited) as error:
        await _next_event(client)
    assert error.value.stderr_tail == "pi diagnostic only\n"
    await client.close()


async def test_non_json_stdout_fails_immediately_as_protocol_error(
    fake_process: tuple[FakeProcess, list[tuple[Any, ...]], list[dict[str, Any]]],
) -> None:
    process, _, _ = fake_process
    client = await PiRpcClient.start(_config())

    process.stdout.feed(b"not rpc json\n")

    with pytest.raises(PiRpcProtocolError):
        await _next_event(client)
    await client.close()


async def test_wall_clock_timeout_terminates_process(
    fake_process: tuple[FakeProcess, list[tuple[Any, ...]], list[dict[str, Any]]],
) -> None:
    process, _, _ = fake_process
    client = await PiRpcClient.start(_config(timeout_seconds=0.01))

    with pytest.raises(PiRpcTimeout):
        await _next_event(client)

    assert process.terminated
    await client.close()


async def test_abort_writes_rpc_abort_then_waits_for_process_exit(
    fake_process: tuple[FakeProcess, list[tuple[Any, ...]], list[dict[str, Any]]],
) -> None:
    process, _, _ = fake_process
    client = await PiRpcClient.start(_config())

    aborting = asyncio.create_task(client.abort())
    await asyncio.sleep(0)
    request = json.loads(process.stdin.writes[0])
    assert request["type"] == "abort"
    assert isinstance(request["id"], str)
    process.exit(0)

    await aborting


async def test_close_terminates_process_and_cleans_per_run_directory(
    fake_process: tuple[FakeProcess, list[tuple[Any, ...]], list[dict[str, Any]]],
) -> None:
    process, _, kwargs_calls = fake_process
    client = await PiRpcClient.start(_config())
    agent_dir = Path(kwargs_calls[0]["env"]["PI_CODING_AGENT_DIR"])

    await client.close()

    assert process.terminated
    assert process.stdin.closed
    assert not agent_dir.exists()
