import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from app.pi_runtime_poc import rpc as rpc_module
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
    def __init__(self, *, stubborn: bool = False) -> None:
        self.stdin = FakeStdin()
        self.stdout = FakeReader()
        self.stderr = FakeReader()
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self._stubborn = stubborn
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
        if not self._stubborn:
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


# --- Fix round 1：环境最小 allowlist（Critical） ---


async def test_spawn_env_does_not_inherit_host_secrets(
    fake_process: tuple[FakeProcess, list[tuple[Any, ...]], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """预置宿主敏感环境变量，断言它们绝不被整体继承进 Pi 子进程环境。"""
    host_env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "MYSQL_PASSWORD": "host-db-secret",
        "DATATAP_MCP_TOKEN": "host-datatap-secret",
        "TENCENT_PLAN_API_KEY": "host-model-key",
        "JWT_SECRET": "host-jwt-secret",
        "PI_RUNTIME_POC_INTERNAL_SECRET": "host-internal-secret",
    }
    monkeypatch.setattr(os, "environ", dict(host_env))
    _, _, kwargs_calls = fake_process
    client = await PiRpcClient.start(_config())
    await client.close()

    env = kwargs_calls[0]["env"]
    for secret_key in (
        "MYSQL_PASSWORD",
        "DATATAP_MCP_TOKEN",
        "TENCENT_PLAN_API_KEY",
        "JWT_SECRET",
        "PI_RUNTIME_POC_INTERNAL_SECRET",
    ):
        assert secret_key not in env, f"宿主敏感变量 {secret_key} 泄漏进 Pi 子进程环境"
    # allowlist 内的非敏感变量仍可用
    assert env["PATH"] == "/usr/local/bin:/usr/bin:/bin"
    assert env["LANG"] == "en_US.UTF-8"


async def test_spawn_env_merges_only_explicit_config_environment(
    fake_process: tuple[FakeProcess, list[tuple[Any, ...]], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只有 PiRpcConfig 显式提供的 environment 被并入，其余宿主变量不进入。"""
    monkeypatch.setattr(
        os,
        "environ",
        {
            "PATH": "/usr/bin:/bin",
            "SOME_RANDOM_HOST_VAR": "host-value",
            "UNLISTED_HOST_VAR": "must-not-leak",
        },
    )
    _, _, kwargs_calls = fake_process
    client = await PiRpcClient.start(_config(environment={"DATA_DIR": "/tmp/pi-data"}))
    await client.close()

    env = kwargs_calls[0]["env"]
    assert env["DATA_DIR"] == "/tmp/pi-data"
    # 未显式提供、也不在 allowlist 的宿主变量不得混入
    assert "SOME_RANDOM_HOST_VAR" not in env
    assert "UNLISTED_HOST_VAR" not in env


async def test_spawn_env_preserves_pi_required_keys(
    fake_process: tuple[FakeProcess, list[tuple[Any, ...]], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pi 运行必需键（agent dir / offline / skip version check）恒被注入。"""
    monkeypatch.setattr(os, "environ", {"PATH": "/usr/bin:/bin"})
    _, _, kwargs_calls = fake_process
    client = await PiRpcClient.start(_config())
    await client.close()

    env = kwargs_calls[0]["env"]
    assert env["PI_OFFLINE"] == "1"
    assert env["PI_SKIP_VERSION_CHECK"] == "1"
    assert Path(env["PI_CODING_AGENT_DIR"]).name.startswith("kol-insight-pi-rpc-")
    assert not Path(env["PI_CODING_AGENT_DIR"]).exists()


# --- Fix round 1：abort 不无限等待（Important） ---


async def test_abort_terminates_process_that_does_not_exit(
    fake_process: tuple[FakeProcess, list[tuple[Any, ...]], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """发送 abort 后进程不退出：短暂宽限后必须 terminate 当前精确 PID，且 abort 有限时返回。"""
    monkeypatch.setattr(rpc_module, "_ABORT_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(rpc_module, "_TERMINATE_GRACE_SECONDS", 0.01)
    process, _, _ = fake_process
    client = await PiRpcClient.start(_config())

    await asyncio.wait_for(client.abort(), timeout=5.0)

    assert process.terminated
    assert process.killed is False
    await client.close()


async def test_abort_escalates_to_kill_when_terminate_lingers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """abort 后 terminate 仍不退出的顽固进程：必须升级为 kill，且 abort 不无限等待。"""
    monkeypatch.setattr(rpc_module, "_ABORT_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(rpc_module, "_TERMINATE_GRACE_SECONDS", 0.01)
    process = FakeProcess(stubborn=True)

    async def spawn(*args: Any, **kwargs: Any) -> FakeProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    client = await PiRpcClient.start(_config())

    await asyncio.wait_for(client.abort(), timeout=5.0)

    assert process.terminated
    assert process.killed
    await client.close()


# --- Fix round 1：永不换行 stdout 上限（Minor） ---


async def test_unterminated_record_over_max_bytes_fails_protocol(
    fake_process: tuple[FakeProcess, list[tuple[Any, ...]], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """永不换行的 stdout 超过单条 RPC record 上限必须以协议错误失败，而不是无限缓冲。"""
    monkeypatch.setattr(rpc_module, "_MAX_RPC_RECORD_BYTES", 32)
    process, _, _ = fake_process
    client = await PiRpcClient.start(_config())

    process.stdout.feed(b"x" * 4096)

    with pytest.raises(PiRpcProtocolError, match="rpc_record_too_large"):
        await _next_event(client)
    await client.close()
