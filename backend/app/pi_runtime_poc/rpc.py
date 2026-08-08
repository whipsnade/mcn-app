"""严格隔离的 Pi RPC JSONL 子进程客户端。

本模块只负责 Pi 子进程与其 LF JSONL 边界；调用方负责 POC 配置门禁和业务编排。
"""

import asyncio
import json
import os
import shutil
import tempfile
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_STDERR_TAIL_BYTES = 64 * 1024
_CLOSE_GRACE_SECONDS = 5.0
_CLOSE_READER_GRACE_SECONDS = 2.0
_CLOSE_STDIN_GRACE_SECONDS = 2.0
_ABORT_GRACE_SECONDS = 2.0
_TERMINATE_GRACE_SECONDS = 2.0
_MAX_RPC_RECORD_BYTES = 16 * 1024 * 1024
# 仅这些非敏感变量从宿主环境放行；其余宿主环境绝不整体继承进 Pi 子进程。
_ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "LANGUAGE", "TMPDIR", "HOME")
_END = object()


class PiRpcProtocolError(RuntimeError):
    """Pi stdout 违反严格 JSONL RPC 契约。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class PiRpcTimeout(RuntimeError):
    """Pi Run 超过整个子进程的墙钟时限。"""


class PiRpcExited(RuntimeError):
    """Pi 在未请求关闭时退出。"""

    def __init__(self, exit_code: int, stderr_tail: str) -> None:
        super().__init__(f"pi_rpc_exited:{exit_code}")
        self.exit_code = exit_code
        self.stderr_tail = stderr_tail


@dataclass(frozen=True, slots=True)
class PiRpcConfig:
    """仅接受已审核的 Pi 二进制和显式资源路径。"""

    executable: str
    extensions: Sequence[str] = ()
    skills: Sequence[str] = ()
    timeout_seconds: float = 30 * 60
    environment: Mapping[str, str] | None = None
    # 仅由调用方生成的非敏感 Agent 配置（目前仅 models.json）；写入本 Run 的
    # 临时目录，进程退出后随目录删除。不得把 API key 写入这里。
    agent_files: Mapping[str, str] | None = None
    provider: str | None = None
    model: str | None = None
    thinking: str | None = None
    cwd: str | None = None


class PiRpcClient:
    """一个 Run 一个 Pi RPC 子进程，stdout 仅接受严格 LF JSONL。"""

    def __init__(
        self,
        *,
        process: asyncio.subprocess.Process,
        agent_dir: Path,
        timeout_seconds: float,
    ) -> None:
        self._process = process
        self._agent_dir = agent_dir
        self._timeout_seconds = timeout_seconds
        self._events: asyncio.Queue[dict[str, Any] | object] = asyncio.Queue()
        self._write_lock = asyncio.Lock()
        self._stderr_tail = bytearray()
        self._fatal: Exception | None = None
        self._closed = False
        self._abort_requested = False
        self._terminal_sent = False
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._process_wait_task: asyncio.Task[int] | None = None
        self._wait_task: asyncio.Task[None] | None = None
        self._deadline_task: asyncio.Task[None] | None = None

    @classmethod
    async def start(cls, config: PiRpcConfig) -> "PiRpcClient":
        """启动隔离进程；绝不通过 shell 拼接命令。"""

        if not config.executable:
            raise ValueError("pi_executable_required")
        if config.timeout_seconds <= 0:
            raise ValueError("pi_timeout_must_be_positive")

        agent_dir = Path(tempfile.mkdtemp(prefix="kol-insight-pi-rpc-"))
        try:
            _write_agent_files(agent_dir, config.agent_files)
            environment = _pi_environment(agent_dir, config.environment)
            args = _command_args(config)
            process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
                cwd=config.cwd,
            )
        except Exception:
            shutil.rmtree(agent_dir, ignore_errors=True)
            raise

        if process.stdin is None or process.stdout is None or process.stderr is None:
            shutil.rmtree(agent_dir, ignore_errors=True)
            await _stop_started_process(process)
            raise PiRpcExited(process.returncode if process.returncode is not None else -1, "")

        client = cls(process=process, agent_dir=agent_dir, timeout_seconds=config.timeout_seconds)
        client._stdout_task = asyncio.create_task(client._read_stdout())
        client._stderr_task = asyncio.create_task(client._read_stderr())
        client._process_wait_task = asyncio.create_task(process.wait())
        client._wait_task = asyncio.create_task(client._wait_for_exit())
        client._deadline_task = asyncio.create_task(client._enforce_deadline())
        return client

    async def prompt(self, message: str) -> str:
        """发送一个带调用方可关联 request id 的 Pi prompt。"""

        if not isinstance(message, str) or not message:
            raise ValueError("pi_prompt_required")
        request_id = _request_id()
        await self._write({"id": request_id, "type": "prompt", "message": message})
        return request_id

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        """按 Pi stdout 到达顺序产出事件；终态错误在此边界抛出。"""

        while True:
            item = await self._events.get()
            if item is _END:
                if self._fatal is not None:
                    raise self._fatal
                return
            assert isinstance(item, dict)
            yield item

    async def abort(self) -> None:
        """请求 Pi abort，并确保本 Run 的精确子进程有限时退出，避免遗留孤儿进程。"""

        if self._closed or self._process.returncode is not None:
            return
        self._abort_requested = True
        await self._write({"id": _request_id(), "type": "abort"})
        if self._wait_task is None:
            return
        # 1) 短暂宽限让 Pi 自主处理 abort 后退出
        await self._await_exit_within(_ABORT_GRACE_SECONDS)
        # 2) 仍未退出：仅操作当前精确 PID 的 terminate
        if self._process.returncode is None:
            self._process.terminate()
            await self._await_exit_within(_TERMINATE_GRACE_SECONDS)
        # 3) 仍未退出：仅操作当前精确 PID 的 kill
        if self._process.returncode is None:
            self._process.kill()
            await self._await_exit_within(_TERMINATE_GRACE_SECONDS)

    async def _await_exit_within(self, seconds: float) -> None:
        if self._process_wait_task is None or self._process.returncode is not None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(self._process_wait_task), timeout=seconds)
        except TimeoutError:
            pass

    async def close(self) -> None:
        """终止仍在运行的子进程并清理此 Run 的临时资源目录。"""

        if self._closed:
            return
        self._closed = True
        current_task = asyncio.current_task()
        if self._deadline_task is not None and self._deadline_task is not current_task:
            self._deadline_task.cancel()

        try:
            if self._process.returncode is None:
                self._process.terminate()
            await self._await_exit_within(_CLOSE_GRACE_SECONDS)
            if self._process.returncode is None:
                self._process.kill()
                await self._await_exit_within(_CLOSE_GRACE_SECONDS)

            await self._close_task_within(self._stdout_task, _CLOSE_READER_GRACE_SECONDS, current_task)
            await self._close_task_within(self._stderr_task, _CLOSE_READER_GRACE_SECONDS, current_task)
            await self._close_task_within(self._wait_task, _CLOSE_READER_GRACE_SECONDS, current_task)
            await self._close_stdin_within(current_task)
        finally:
            shutil.rmtree(self._agent_dir, ignore_errors=True)

    async def _close_task_within(
        self,
        task: asyncio.Task[Any] | None,
        timeout_seconds: float,
        current_task: asyncio.Task[Any] | None,
    ) -> None:
        if task is None or task is current_task or task.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout_seconds)
            return
        except TimeoutError:
            task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout_seconds)
        except (TimeoutError, asyncio.CancelledError):
            return

    async def _close_stdin_within(self, current_task: asyncio.Task[Any] | None) -> None:
        if self._process.stdin is None:
            return
        self._process.stdin.close()
        wait_closed = getattr(self._process.stdin, "wait_closed", None)
        if wait_closed is None:
            return
        task = asyncio.ensure_future(wait_closed())
        if task is current_task:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=_CLOSE_STDIN_GRACE_SECONDS)
        except TimeoutError:
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=_CLOSE_STDIN_GRACE_SECONDS)
            except (TimeoutError, asyncio.CancelledError):
                return

    async def _write(self, payload: dict[str, Any]) -> None:
        if self._fatal is not None:
            raise self._fatal
        if self._closed or self._process.returncode is not None:
            raise PiRpcExited(self._process.returncode if self._process.returncode is not None else -1, "")
        assert self._process.stdin is not None
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        async with self._write_lock:
            try:
                self._process.stdin.write(encoded)
                await self._process.stdin.drain()
            except (BrokenPipeError, ConnectionError) as exc:
                error = PiRpcExited(
                    self._process.returncode if self._process.returncode is not None else -1,
                    self._stderr_text(),
                )
                await self._fail(error)
                raise error from exc

    async def _read_stdout(self) -> None:
        assert self._process.stdout is not None
        buffer = bytearray()
        try:
            while chunk := await self._process.stdout.read(4096):
                buffer.extend(chunk)
                if len(buffer) > _MAX_RPC_RECORD_BYTES:
                    await self._fail(PiRpcProtocolError("pi_rpc_record_too_large"))
                    return
                while (line_end := buffer.find(b"\n")) >= 0:
                    line = bytes(buffer[:line_end])
                    del buffer[: line_end + 1]
                    if line.endswith(b"\r"):
                        line = line[:-1]
                    await self._events.put(_parse_record(line))
            if buffer:
                await self._fail(PiRpcProtocolError("pi_rpc_unterminated_record"))
        except asyncio.CancelledError:
            raise
        except PiRpcProtocolError as exc:
            await self._fail(exc)
        except UnicodeDecodeError:
            await self._fail(PiRpcProtocolError("pi_rpc_invalid_utf8"))
            raise

    async def _read_stderr(self) -> None:
        assert self._process.stderr is not None
        while chunk := await self._process.stderr.read(4096):
            self._stderr_tail.extend(chunk)
            if len(self._stderr_tail) > _STDERR_TAIL_BYTES:
                del self._stderr_tail[: len(self._stderr_tail) - _STDERR_TAIL_BYTES]

    async def _wait_for_exit(self) -> None:
        if self._process_wait_task is None:
            raise RuntimeError("pi_rpc_process_wait_task_missing")
        exit_code = await self._process_wait_task
        if self._stdout_task is not None:
            await self._stdout_task
        if self._stderr_task is not None:
            await self._stderr_task
        if not self._closed and not self._abort_requested and self._fatal is None:
            await self._fail(PiRpcExited(exit_code, self._stderr_text()))
        else:
            await self._finish_events()

    async def _enforce_deadline(self) -> None:
        try:
            await asyncio.sleep(self._timeout_seconds)
        except asyncio.CancelledError:
            return
        if not self._closed and self._process.returncode is None:
            await self._fail(PiRpcTimeout("pi_rpc_wall_clock_timeout"))

    async def _fail(self, error: Exception) -> None:
        if self._fatal is not None:
            return
        self._fatal = error
        if self._process.returncode is None:
            self._process.terminate()
        await self._finish_events()

    async def _finish_events(self) -> None:
        if self._terminal_sent:
            return
        self._terminal_sent = True
        await self._events.put(_END)

    def _stderr_text(self) -> str:
        return bytes(self._stderr_tail).decode("utf-8", errors="replace")


def _pi_environment(
    agent_dir: Path,
    explicit: Mapping[str, str] | None,
) -> dict[str, str]:
    """构造 Pi 子进程环境：仅 allowlist 宿主非敏感变量 + 显式配置 + Pi 必需键。

    绝不整体继承宿主环境，避免数据库密码、DataTap token、模型 key、
    内部签名 secret 等因环境继承进入 Pi。
    """
    env = {key: os.environ[key] for key in _ENV_ALLOWLIST if key in os.environ}
    if explicit:
        env.update(explicit)
    env.update(
        {
            "PI_CODING_AGENT_DIR": str(agent_dir),
            "PI_OFFLINE": "1",
            "PI_SKIP_VERSION_CHECK": "1",
        }
    )
    return env


def _write_agent_files(agent_dir: Path, files: Mapping[str, str] | None) -> None:
    """只在本 Run 临时 Agent 目录写入显式、非敏感配置文件。"""
    for relative_name, content in (files or {}).items():
        path = Path(relative_name)
        if (
            not isinstance(content, str)
            or path.is_absolute()
            or ".." in path.parts
            or len(path.parts) != 1
        ):
            raise ValueError("invalid_pi_agent_file")
        target = agent_dir / path
        target.write_text(content, encoding="utf-8")
        target.chmod(0o600)


def _command_args(config: PiRpcConfig) -> list[str]:
    args = [
        config.executable,
        "--mode",
        "rpc",
        "--no-session",
        "--no-builtin-tools",
        "--no-context-files",
        "--no-extensions",
    ]
    for extension in config.extensions:
        args.extend(("-e", extension))
    args.append("--no-skills")
    for skill in config.skills:
        args.extend(("--skill", skill))
    if config.provider is not None:
        args.extend(("--provider", config.provider))
    if config.model is not None:
        args.extend(("--model", config.model))
    if config.thinking is not None:
        args.extend(("--thinking", config.thinking))
    return args


def _parse_record(line: bytes) -> dict[str, Any]:
    if not line.strip():
        raise PiRpcProtocolError("pi_rpc_invalid_record")
    try:
        value = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PiRpcProtocolError("pi_rpc_invalid_record") from exc
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        raise PiRpcProtocolError("pi_rpc_invalid_record")
    return _project_rpc_event(value)


def _project_rpc_event(event: dict[str, Any]) -> dict[str, Any]:
    """在 Queue 前丢弃 Pi 的累积消息快照和供应商内容。"""
    event_type = event["type"]
    if event_type == "agent_end":
        messages = event.get("messages")
        will_retry = event.get("willRetry")
        return {
            "type": "agent_end",
            "willRetry": will_retry if isinstance(will_retry, bool) else None,
            "messageCount": len(messages) if isinstance(messages, list) else 0,
        }
    if event_type == "message_update":
        update = event.get("assistantMessageEvent")
        if not isinstance(update, dict):
            return {"type": "message_update"}
        projected: dict[str, Any] = {"type": update.get("type")}
        delta = update.get("delta")
        if isinstance(delta, str):
            projected["delta"] = delta
        return {"type": "message_update", "assistantMessageEvent": projected}
    if event_type in {"tool_execution_start", "tool_execution_end", "tool_execution_update"}:
        return {
            "type": event_type,
            "toolCallId": event.get("toolCallId"),
            "toolName": event.get("toolName"),
            "isError": event.get("isError") is True,
        }
    if event_type == "response":
        projected = {
            "type": "response",
            "command": event.get("command"),
            "success": event.get("success") is True,
        }
        if isinstance(event.get("id"), str):
            projected["id"] = event["id"]
        return projected
    if event_type in {"agent_start", "turn_start", "turn_end", "message_start", "message_end", "error"}:
        return {"type": event_type}
    return {"type": event_type}


def _request_id() -> str:
    return f"pi-{uuid.uuid4()}"


async def _stop_started_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        process.terminate()
    await process.wait()
