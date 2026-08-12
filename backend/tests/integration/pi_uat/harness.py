"""离线进程级 UAT 拓扑编排：真实进程 + 真实 HTTP + 隔离测试库。

启动组件（全部 loopback、零外部网络）：
- fake OpenAI 兼容模型（进程内 uvicorn task）
- fake DataTap MCP × 2（进程内 uvicorn task，真实 Streamable HTTP MCP）
- FastAPI 后端（子进程，真实 lifespan/恢复循环/内部协议）
- 生产 Pi Gateway（子进程，``node dist/main.js``）

数据库固定 ``kol_insight_test``；所有种子数据在 teardown 精确删除。
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

import httpx
import uvicorn
from sqlalchemy import delete, select, text, update
from sse_starlette.sse import AppStatus

from app.agent_runtime.models import AgentRun
from app.db.session import SessionFactory
from app.identity.models import AuthIdentity, User
from app.licensing.models import TenantLicense
from app.tenancy.models import Tenant, TenantMembership

from .fake_mcp import FakeDataTapService, datatap_gateway_app
from .fake_model import FakeModelServer


BACKEND_DIR = Path(__file__).resolve().parents[3]
WORKTREE_ROOT = BACKEND_DIR.parent
GATEWAY_DIR = WORKTREE_ROOT / "pi-gateway"
GATEWAY_SECRET = "uat-only-gateway-secret-0123456789abcdef"
GATEWAY_ID = "gw-uat-1"
MASTER_KEY_B64 = "dXV1dXV1dXV1dXV1dXV1dXV1dXV1dXV1dXV1dXV1dXU="  # base64(32 bytes, 测试占位)


def assert_uat_database_scope() -> None:
    """Destructive UAT cleanup is allowed only in the dedicated test database."""
    expected = {
        "APP_ENV": "test",
        "MYSQL_DATABASE": "kol_insight_test",
        "MYSQL_USER": "kol_test",
    }
    if any(os.environ.get(key) != value for key, value in expected.items()):
        raise RuntimeError("uat_database_scope_invalid")


async def assert_uat_database_connection(db) -> None:
    """Verify the actual connection, not only caller-controlled environment vars."""
    assert_uat_database_scope()
    database, current_user = (
        await db.execute(text("SELECT DATABASE(), CURRENT_USER()"))
    ).one()
    user_name = str(current_user).split("@", 1)[0]
    if str(database) != "kol_insight_test" or user_name != "kol_test":
        raise RuntimeError("uat_database_connection_scope_invalid")


@dataclass
class UatUser:
    user_id: str
    phone: str
    token: str = ""


@dataclass
class UatTenant:
    tenant_id: str
    slug: str
    users: list[UatUser] = field(default_factory=list)


async def purge_uat_residue() -> None:
    """清除任何已提交 UAT/agent-real 链路残留（崩溃/中断运行的种子行）。

    迁移类测试（0037/0040/0043 guard）要求全库无此类残留；正常 teardown
    已逐拓扑清理，这里兜底中断场景。仅限隔离测试库。
    """
    assert_uat_database_scope()
    from app.agent_runtime.models import AgentRun, AgentSession
    from app.identity.models import User
    from app.tenancy.models import Tenant

    async with SessionFactory.begin() as db:
        await assert_uat_database_connection(db)
        await db.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        try:
            user_ids = list(
                (
                    await db.scalars(
                        select(User.id).where(User.nickname.like("uat-%"))
                    )
                ).all()
            )
            tenant_ids = list(
                (
                    await db.scalars(
                        select(Tenant.id).where(Tenant.slug.like("uat-tenant-%"))
                    )
                ).all()
            )
            session_ids = list(
                (
                    await db.scalars(
                        select(AgentSession.id).where(AgentSession.user_id.in_(user_ids or [""]))
                    )
                ).all()
            )
            run_ids = list(
                (
                    await db.scalars(
                        select(AgentRun.id).where(AgentRun.session_id.in_(session_ids or [""]))
                    )
                ).all()
            )
            for table, column, ids in (
                ("artifact_publish_attempts", "run_id", run_ids),
                ("artifact_draft_revisions", "run_id", run_ids),
                ("runtime_usage_records", "run_id", run_ids),
                ("agent_tool_call_reconciliations", "run_id", run_ids),
                ("agent_tool_calls", "run_id", run_ids),
                ("agent_steps", "run_id", run_ids),
                ("agent_run_attempts", "run_id", run_ids),
                ("agent_events", "run_id", run_ids),
                ("agent_messages", "run_id", run_ids),
                ("agent_runs", "id", run_ids),
                ("runtime_usage_records", "tenant_id", tenant_ids),
                ("tenant_wallet_transactions", "tenant_id", tenant_ids),
                ("tenant_user_quota_usage", "tenant_id", tenant_ids),
                ("tenant_user_quota_policies", "tenant_id", tenant_ids),
                ("tenant_wallets", "tenant_id", tenant_ids),
                ("pi_tenant_queue_states", "tenant_id", tenant_ids),
                ("encrypted_runtime_secrets", "tenant_id", tenant_ids),
                ("runtime_config_versions", "tenant_id", tenant_ids),
                ("tenant_licenses", "tenant_id", tenant_ids),
                ("tenant_memberships", "tenant_id", tenant_ids),
                ("tenants", "id", tenant_ids),
                ("evidence_items", "session_id", session_ids),
                ("agent_artifact_read_states", "session_id", session_ids),
                ("kol_detail_cache", "session_id", session_ids),
                ("memory_entries", "session_id", session_ids),
                ("agent_uploads", "session_id", session_ids),
                ("artifact_events", "session_id", session_ids),
                ("agent_messages", "session_id", session_ids),
                ("agent_sessions", "id", session_ids),
                ("model_prompt_logs", "user_id", user_ids),
                ("wallet_transactions", "user_id", user_ids),
                ("wallets", "user_id", user_ids),
                ("auth_identities", "user_id", user_ids),
                ("users", "id", user_ids),
            ):
                if ids:
                    await db.execute(
                        text(f"DELETE FROM {table} WHERE {column} IN ({','.join(repr(i) for i in ids)})")
                    )
            # artifact 侧（经 session → artifact 关联）
            if session_ids:
                await db.execute(
                    text(
                        "DELETE av FROM agent_artifact_versions av "
                        "JOIN agent_artifacts a ON av.artifact_id = a.id "
                        f"WHERE a.session_id IN ({','.join(repr(i) for i in session_ids)})"
                    )
                )
                await db.execute(
                    text(
                        f"DELETE FROM agent_artifacts WHERE session_id IN ({','.join(repr(i) for i in session_ids)})"
                    )
                )
            await db.execute(text("DELETE FROM pi_gateway_instances WHERE gateway_id LIKE 'gw-uat-%'"))
            await db.execute(text("DELETE FROM pi_gateway_request_nonces WHERE gateway_id LIKE 'gw-uat-%'"))
        finally:
            await db.execute(text("SET FOREIGN_KEY_CHECKS=1"))


async def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


_GATEWAY_BUILT = False


def _ensure_gateway_built() -> None:
    """每个测试会话一次：从当前 HEAD 的 src 构建 Gateway（dist 过期才重建）。"""
    global _GATEWAY_BUILT
    if _GATEWAY_BUILT:
        return
    src_dir = GATEWAY_DIR / "src"
    dist_main = GATEWAY_DIR / "dist" / "main.js"
    newest_src = max((path.stat().st_mtime for path in src_dir.glob("*.ts")), default=0.0)
    if not dist_main.exists() or dist_main.stat().st_mtime < newest_src:
        subprocess.run(
            ["npm", "run", "build"],
            cwd=str(GATEWAY_DIR),
            check=True,
            capture_output=True,
            text=True,
        )
    _GATEWAY_BUILT = True


class _InProcessServer:
    _active_servers: ClassVar[set[_InProcessServer]] = set()

    def __init__(self, app, port: int) -> None:
        # timeout_graceful_shutdown=2：step_hang 场景 teardown 时模型服务器还有
        # 长 sleep 的悬挂请求（客户端进程已消失），优雅停机 2s 后强制取消，
        # 避免 serve 任务卡满 stop() 的 10s 上限。
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            timeout_graceful_shutdown=2,
        )
        self._server = uvicorn.Server(config)
        self._task: asyncio.Task | None = None
        self._tasks_before_start: set[asyncio.Task[object]] = set()

    async def start(self) -> None:
        # sse-starlette keeps a per-event-loop shutdown watcher. In-process
        # Uvicorn servers do not install a durable process signal handler, so
        # reset the test-loop flag before each fresh server.
        AppStatus.should_exit = False
        current = asyncio.current_task()
        self._tasks_before_start = {
            task for task in asyncio.all_tasks() if task is not current
        }
        self._task = asyncio.create_task(self._server.serve())
        for _ in range(100):
            if self._server.started:
                self._active_servers.add(self)
                return
            await asyncio.sleep(0.05)
        raise RuntimeError("in_process_server_start_timeout")

    async def stop(self) -> None:
        # Signal SSE responses owned by this in-process server before asking
        # Uvicorn to drain. Otherwise the watcher can outlive the server task
        # and poison the next topology in the same event loop.
        AppStatus.should_exit = True
        self._server.should_exit = True
        task = self._task
        if task is None:
            self._active_servers.discard(self)
            if not self._active_servers:
                AppStatus.should_exit = False
            return
        try:
            done, _ = await asyncio.wait({task}, timeout=2)
            if done:
                _consume_task_result(task)
                await _drain_sse_shutdown_watchers(self._tasks_before_start)
                return
            # Uvicorn may still be draining a deliberately hanging fake request.
            # Cancel and gather the serve task with a second bounded wait. A task
            # that swallows cancellation must fail closed instead of blocking DB
            # teardown forever.
            task.cancel()
            await _reap_tasks_bounded(
                [task], timeout=2, error_code="in_process_server_stop_timeout"
            )
            await _drain_sse_shutdown_watchers(self._tasks_before_start)
        finally:
            self._active_servers.discard(self)
            if not self._active_servers:
                AppStatus.should_exit = False


def _consume_task_result(task: asyncio.Task[object]) -> None:
    """Retrieve task exceptions without letting cleanup rewrite the root error."""
    if task.cancelled():
        return
    try:
        task.exception()
    except BaseException:
        pass


async def _reap_tasks_bounded(
    tasks: list[asyncio.Task[object]], *, timeout: float, error_code: str
) -> None:
    """Await tasks, then cancel and await them again within a hard deadline."""
    if not tasks:
        return
    joined = asyncio.gather(*tasks, return_exceptions=True)
    done, _ = await asyncio.wait({joined}, timeout=timeout)
    if not done:
        for task in tasks:
            if not task.done():
                task.cancel()
        done, _ = await asyncio.wait({joined}, timeout=timeout)
    if not done or any(not task.done() for task in tasks):
        raise RuntimeError(error_code)
    if not joined.cancelled():
        try:
            joined.result()
        except BaseException:
            pass
    for task in tasks:
        _consume_task_result(task)


async def _wait_proc_exit(proc: subprocess.Popen, timeout: float) -> bool:
    """非阻塞等子进程退出；proc.wait() 会冻结事件循环，饿死同进程的 fake 服务。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return True
        await asyncio.sleep(0.1)
    return False


async def _wait_pgid_gone(pgid: int, timeout: float) -> bool:
    """Poll one recorded process group without blocking the event loop."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except PermissionError as exc:
            raise RuntimeError("uat_process_group_probe_denied") from exc
        await asyncio.sleep(0.1)
    return False


async def _drain_sse_shutdown_watchers(
    tasks_before_start: set[asyncio.Task[object]], timeout: float = 2.0
) -> None:
    """收口 sse-starlette 为进程内 Uvicorn 创建的 shutdown watcher。

    UAT 的 fake MCP 使用 Streamable HTTP；其 SSE response 会在当前事件循环
    创建一个全局 watcher，而 in-process Uvicorn 不会留下可复用的 signal
    handler。仅等待 ``Server.serve`` 完成会让 watcher 越过拓扑边界，下一轮
    启动又把全局退出标志重置，最终卡住新的 list_tools。这里只处理本测试
    该 server 启动后创建、明确属于它的 SSE watcher，超过有界时间才取消并完整 gather。
    """
    deadline = time.monotonic() + timeout
    current = asyncio.current_task()

    def watchers() -> list[asyncio.Task[object]]:
        result: list[asyncio.Task[object]] = []
        for task in asyncio.all_tasks():
            if task is current or task.done():
                continue
            if (
                task not in tasks_before_start
                and task.get_coro().__qualname__ == "_shutdown_watcher"
            ):
                result.append(task)
        return result

    while time.monotonic() < deadline:
        pending = watchers()
        if not pending:
            return
        await asyncio.sleep(0.05)
    pending = watchers()
    for task in pending:
        task.cancel()
    await _reap_tasks_bounded(
        pending, timeout=timeout, error_code="sse_shutdown_watcher_cleanup_timeout"
    )
    if watchers():
        raise RuntimeError("sse_shutdown_watcher_cleanup_timeout")


async def _wait_http_ok(url: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient() as client:
        while time.monotonic() < deadline:
            try:
                response = await client.get(url, timeout=2)
                if response.status_code == 200:
                    return
            except Exception:
                pass
            await asyncio.sleep(0.2)
    raise RuntimeError(f"http_ready_timeout:{url}")


class PiUatTopology:
    """完整离线拓扑的装配与清理；全部断言可经 DB/HTTP 外部观测。"""

    def __init__(self, *, scripts: dict[str, list[dict]] | None = None, kill_switch: bool = False) -> None:
        self.model = FakeModelServer(scripts)
        self.mcp_services = [FakeDataTapService("insight-cube-mcp"), FakeDataTapService("social-grow-mcp")]
        self.kill_switch = kill_switch
        self.gateway_id = f"gw-uat-{uuid4().hex[:12]}"
        self.lifecycle_events: list[str] = []
        self._servers: list[_InProcessServer] = []
        self._fastapi: subprocess.Popen | None = None
        self._gateway: subprocess.Popen | None = None
        self._fastapi_pgid: int | None = None
        self._gateway_pgid: int | None = None
        self.gateway_log: Path | None = None
        self.model_port = 0
        self.fastapi_port = 0
        self.gateway_health_port = 0
        self.mcp_origin = ""
        self.mcp_urls: dict[str, str] = {}
        self.tenants: dict[str, UatTenant] = {}
        self.admin_token = ""
        self._seeded_user_ids: list[str] = []
        self._seeded_tenant_ids: list[str] = []
        self._cleanup_task: asyncio.Task | None = None

    def _log_lifecycle(self, event: str) -> None:
        self.lifecycle_events.append(event)

    @property
    def api_base(self) -> str:
        return f"http://127.0.0.1:{self.fastapi_port}"

    @property
    def gateway_pgid(self) -> int | None:
        """本拓扑 Gateway 的进程组 id（worker 子进程 fork 自同组）。"""
        if self._gateway is None or self._gateway.poll() is not None:
            return None
        return self._gateway_pgid

    async def __aenter__(self) -> PiUatTopology:
        try:
            return await self._start_topology()
        except BaseException:
            # 启动中途失败（例如 healthz 等待超时）也必须回收已拉起的子进程
            # 与已提交的种子数据，否则泄漏的 FastAPI/Gateway 会继续在共享
            # 测试库上窃取后续拓扑的 Run，残留租户会阻断迁移 guard。
            await self._cleanup()
            raise

    async def _start_topology(self) -> PiUatTopology:
        _ensure_gateway_built()
        self.model_port = await _free_port()
        self.fastapi_port = await _free_port()
        self.gateway_health_port = await _free_port()
        model_server = _InProcessServer(self.model.app(), self.model_port)
        await model_server.start()
        self._servers.append(model_server)
        self._log_lifecycle("fake_model_ready")
        mcp_port = await _free_port()
        mcp_server = _InProcessServer(datatap_gateway_app(self.mcp_services), mcp_port)
        await mcp_server.start()
        self._servers.append(mcp_server)
        self._log_lifecycle("fake_mcp_ready")
        self.mcp_origin = f"http://127.0.0.1:{mcp_port}"
        for service in self.mcp_services:
            self.mcp_urls[service.service] = (
                f"{self.mcp_origin}/api/gateway/{service.service}/mcp"
            )
        await self._seed_db()
        self._log_lifecycle("db_seeded")
        self._start_fastapi()
        await _wait_http_ok(f"{self.api_base}/healthz")
        self._log_lifecycle("fastapi_ready")
        await self._approve_catalog_rows()
        await self._login_users()
        self._start_gateway()
        await _wait_http_ok(f"http://127.0.0.1:{self.gateway_health_port}/healthz")
        self._log_lifecycle("gateway_ready")
        return self

    async def _stop_processes(self) -> None:
        """按进程组终止本拓扑拉起的子进程（幂等，可重复调用）。

        FastAPI/Gateway 都以独立进程组启动（start_new_session），终止只打
        本拓扑的 pgid，绝不触碰其他会话/拓扑的同名进程。Gateway 的 worker
        子进程由 fork 产生，与 Gateway 同组，一并随组终止。
        """
        for attr, pgid_attr in (
            ("_gateway", "_gateway_pgid"),
            ("_fastapi", "_fastapi_pgid"),
        ):
            proc = getattr(self, attr)
            pgid = getattr(self, pgid_attr)
            if proc is None or pgid is None:
                continue
            await self._stop_process_group(proc, pgid)
            setattr(self, pgid_attr, None)
            self._log_lifecycle("process_reaped")

    async def _stop_process_group(self, proc: subprocess.Popen, pgid: int) -> None:
        """终止并确认一个本拓扑记录的 PGID，即使其父进程已退出。"""
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return
        if proc.poll() is None and not await _wait_proc_exit(proc, 10):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if await _wait_pgid_gone(pgid, 5):
            return
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            return
        if not await _wait_pgid_gone(pgid, 5):
            raise RuntimeError("uat_process_group_reap_timeout")

    async def __aexit__(self, *exc) -> None:
        await self._cleanup()

    async def _cleanup_impl(self) -> None:
        self._log_lifecycle("cleanup_started")
        errors: list[BaseException] = []
        try:
            await self._stop_processes()
        except BaseException as exc:
            errors.append(exc)
        try:
            results = await asyncio.gather(
                *(server.stop() for server in self._servers),
                return_exceptions=True,
            )
            errors.extend(result for result in results if isinstance(result, BaseException))
        except BaseException as exc:
            errors.append(exc)
        finally:
            # ``AppStatus.should_exit`` is a process-global sse-starlette flag;
            # reset it only after every in-process server has been attempted.
            AppStatus.should_exit = False
            try:
                await self._teardown_db()
            except BaseException as exc:
                errors.append(exc)
        if errors:
            raise RuntimeError("uat_cleanup_failed") from errors[0]

    async def _cleanup(self) -> None:
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_impl())
        task = self._cleanup_task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Outer pytest/task cancellation must not cancel cleanup. Drain
            # the shielded task before propagating the original cancellation.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
            await task
            raise

    # ------------------------------------------------------------------ #
    # 子进程
    # ------------------------------------------------------------------ #

    def _fastapi_env(self) -> dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),
            "APP_ENV": "test",
            "AUTH_MODE": "mock",
            "MYSQL_HOST": "127.0.0.1",
            "MYSQL_PORT": "3306",
            "MYSQL_DATABASE": "kol_insight_test",
            "MYSQL_USER": "kol_test",
            "MYSQL_PASSWORD": "test-only-password",
            "JWT_SECRET": "test-only-jwt-secret-at-least-32-characters",
            "TENCENT_PLAN_API_KEY": "test-only",
            "TENCENT_PLAN_BASE_URL": f"http://127.0.0.1:{self.model_port}/v1",
            "TENCENT_PLAN_MODEL": "fake-current-model",
            "DATATAP_MCP_TOKEN": "uat-fake-datatap-token",
            "DATATAP_MCP_ORIGIN": self.mcp_origin,
            "PI_GATEWAY_INTERNAL_SECRET": GATEWAY_SECRET,
            "PI_GATEWAY_ALLOWED_IDS": json.dumps([self.gateway_id]),
            "PI_GATEWAY_KILL_SWITCH": "true" if self.kill_switch else "false",
            "RUNTIME_SECRET_MASTER_KEYS": f"v1:{MASTER_KEY_B64}",
            "RUNTIME_SECRET_ACTIVE_KEY_VERSION": "v1",
            "AGENT_RECOVERY_INTERVAL_SECONDS": "1",
            # 缩短 gateway 租约：worker 崩溃场景依赖「心跳停止 → 租约过期 →
            # 恢复循环接管」，默认 60s 会让单次崩溃恢复等待过久；500ms 心跳
            # 间隔下 5s 租约对正常路径无影响。
            "PI_GATEWAY_LEASE_SECONDS": "5",
            "AGENT_UPLOAD_STORAGE_DIR": f"/tmp/pi-uat-uploads-{uuid4().hex[:8]}",
            "AGENT_EXPORT_STORAGE_DIR": f"/tmp/pi-uat-exports-{uuid4().hex[:8]}",
        }
        # 临时调试：透传 _map_error 的异常落盘路径（scratch 复现用，跑完即还原）
        if os.environ.get("PI_UAT_MAP_ERROR_TRACE"):
            env["PI_UAT_MAP_ERROR_TRACE"] = os.environ["PI_UAT_MAP_ERROR_TRACE"]
        return env

    def _start_fastapi(self) -> None:
        log = Path(f"/tmp/pi-uat-fastapi-{uuid4().hex[:8]}.log")
        self.fastapi_log = log
        self._fastapi = subprocess.Popen(
            [
                str(BACKEND_DIR / ".venv/bin/python"),
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.fastapi_port),
            ],
            cwd=str(BACKEND_DIR),
            env=self._fastapi_env(),
            stdout=log.open("w"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self._fastapi_pgid = self._fastapi.pid
        self._log_lifecycle("fastapi_spawned")

    def _start_gateway(self) -> None:
        log = Path(f"/tmp/pi-uat-gateway-{uuid4().hex[:8]}.log")
        self.gateway_log = log
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),
            "PI_GATEWAY_ID": self.gateway_id,
            "PI_GATEWAY_CONTROL_PLANE_URL": self.api_base,
            "PI_GATEWAY_INTERNAL_SECRET": GATEWAY_SECRET,
            "PI_GATEWAY_ENVIRONMENT": "test",
            "PI_GATEWAY_CAPACITY": "2",
            "PI_GATEWAY_HEALTH_PORT": str(self.gateway_health_port),
            "PI_GATEWAY_CLAIM_INTERVAL_MS": "100",
            "PI_GATEWAY_CLAIM_MAX_BACKOFF_MS": "2000",
            "PI_GATEWAY_HEARTBEAT_INTERVAL_MS": "500",
            "PI_GATEWAY_SHUTDOWN_TIMEOUT_MS": "8000",
        }
        self._gateway = subprocess.Popen(
            ["node", "dist/main.js"],
            cwd=str(GATEWAY_DIR),
            env=env,
            stdout=log.open("w"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self._gateway_pgid = self._gateway.pid
        self._log_lifecycle("gateway_spawned")

    # ------------------------------------------------------------------ #
    # 种子数据
    # ------------------------------------------------------------------ #

    async def _seed_db(self) -> None:
        from app.core.config import get_settings

        os.environ.setdefault("RUNTIME_SECRET_MASTER_KEYS", f"v1:{MASTER_KEY_B64}")
        os.environ.setdefault("RUNTIME_SECRET_ACTIVE_KEY_VERSION", "v1")
        get_settings.cache_clear()
        now = datetime.now(UTC).replace(tzinfo=None)
        async with SessionFactory.begin() as db:
            for slug, backend in (("uat-tenant-a", "pi"), ("uat-tenant-b", "pi")):
                tenant = Tenant(
                    id=str(uuid4()),
                    slug=f"{slug}-{uuid4().hex[:8]}",
                    name=f"UAT {slug}",
                    status="active",
                    is_internal=False,
                    runtime_backend=backend,
                    license_status="active",
                    active_license_id=None,
                    created_at=now,
                    updated_at=now,
                )
                db.add(tenant)
                await db.flush()
                license_row = TenantLicense(
                    id=str(uuid4()),
                    tenant_id=tenant.id,
                    version=1,
                    valid_from=now.replace(microsecond=0),
                    valid_until=None,
                    features_json={
                        "kol_selection": True,
                        "brand_analysis": True,
                        "campaign_analysis": True,
                        "kol_detail": True,
                        "utility": True,
                    },
                    max_concurrent_runs=4,
                    max_user_concurrent_runs=2,
                    created_by="uat-seed",
                    created_at=now,
                )
                db.add(license_row)
                await db.flush()
                tenant.active_license_id = license_row.id
                record = UatTenant(tenant_id=tenant.id, slug=tenant.slug)
                for index in range(2):
                    user = User(
                        id=str(uuid4()),
                        nickname=f"{slug}-user{index}",
                        role="user",
                        status="active",
                        created_at=now,
                        updated_at=now,
                    )
                    db.add(user)
                    await db.flush()
                    phone = f"199{int(uuid4().hex[:8], 16) % 100000000:08d}"
                    db.add(
                        AuthIdentity(
                            id=str(uuid4()),
                            user_id=user.id,
                            provider="sms",
                            provider_subject=phone,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    db.add(
                        TenantMembership(
                            id=str(uuid4()),
                            tenant_id=tenant.id,
                            user_id=user.id,
                            role="owner" if index == 0 else "member",
                            status="active",
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    record.users.append(UatUser(user_id=user.id, phone=phone))
                    self._seeded_user_ids.append(user.id)
                self.tenants[slug] = record
                self._seeded_tenant_ids.append(tenant.id)
            admin = User(
                id=str(uuid4()),
                nickname="uat-admin",
                role="admin",
                status="active",
                created_at=now,
                updated_at=now,
            )
            db.add(admin)
            await db.flush()
            phone = f"188{int(uuid4().hex[:8], 16) % 100000000:08d}"
            db.add(
                AuthIdentity(
                    id=str(uuid4()),
                    user_id=admin.id,
                    provider="sms",
                    provider_subject=phone,
                    created_at=now,
                    updated_at=now,
                )
            )
            self._admin_phone = phone
            self._seeded_user_ids.append(admin.id)

    async def _approve_catalog_rows(self) -> None:
        """启动刷新把 fake 签名隔离后，按 fake 实际 digest 重新审核登记。

        这等价于管理员对新供应商签名的审核动作：digest 以 fake MCP 实际
        上报为准，catalog 与 discovery 保持一致后 claim/preflight 才能绑定。
        """
        from app.mcp_gateway.models import McpToolCatalog, McpToolDiscovery
        from app.mcp_gateway.registry import DYNAMIC_TOOL_ALLOWLIST

        wanted = {
            "insight-cube-mcp": [
                "social_statistic_overview",
                "social_statistic_trend",
                "query_analysis_data",
                "social_statistic_hot_topic",
                "query_raw_posts",
                "social_statistic_user_profile",
            ],
            "social-grow-mcp": ["kol_xiaohongshu_search"],
        }
        now = datetime.now(UTC).replace(tzinfo=None)
        async with SessionFactory.begin() as db:
            for service, names in wanted.items():
                for name in names:
                    discovery = await db.scalar(
                        select(McpToolDiscovery).where(
                            McpToolDiscovery.service_slug == service,
                            McpToolDiscovery.remote_name == name,
                        )
                    )
                    if discovery is None:
                        raise RuntimeError(f"fake discovery row missing for {service}/{name}")
                    discovery.review_status = "approved"
                    discovery.updated_at = now
                    allow = DYNAMIC_TOOL_ALLOWLIST.get(service, {}).get(name)
                    if allow is None:
                        raise RuntimeError(f"allowlist entry missing for {name}")
                    _remote, description, _schema = allow
                    catalog = await db.scalar(
                        select(McpToolCatalog).where(McpToolCatalog.internal_tool_name == name)
                    )
                    if catalog is None:
                        db.add(
                            McpToolCatalog(
                                id=str(uuid4()),
                                service_slug=service,
                                internal_tool_name=name,
                                reviewed_description=description,
                                input_schema_json=discovery.input_schema_json,
                                output_validator_version="v1",
                                discovery_digest=discovery.discovery_digest,
                                review_status="approved",
                                is_enabled=True,
                                created_at=now,
                                updated_at=now,
                            )
                        )
                    else:
                        catalog.service_slug = service
                        catalog.review_status = "approved"
                        catalog.is_enabled = True
                        catalog.discovery_digest = discovery.discovery_digest
                        catalog.updated_at = now

    async def _login_users(self) -> None:
        async with httpx.AsyncClient(base_url=self.api_base, timeout=10) as client:
            for tenant in self.tenants.values():
                for user in tenant.users:
                    response = await client.post(
                        "/api/v1/auth/mock/sms/login",
                        json={"phone": user.phone, "code": "000000"},
                    )
                    response.raise_for_status()
                    user.token = response.json()["access_token"]
            response = await client.post(
                "/api/v1/auth/mock/sms/login",
                json={"phone": self._admin_phone, "code": "000000"},
            )
            response.raise_for_status()
            self.admin_token = response.json()["access_token"]

    # ------------------------------------------------------------------ #
    # 驱动助手
    # ------------------------------------------------------------------ #

    def client_for(self, user: UatUser) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.api_base,
            headers={"Authorization": f"Bearer {user.token}"},
            timeout=30,
        )

    def admin_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.api_base,
            headers={"Authorization": f"Bearer {self.admin_token}"},
            timeout=30,
        )

    async def create_session(self, user: UatUser) -> str:
        async with self.client_for(user) as client:
            response = await client.post("/api/v1/agent/sessions", json={})
            response.raise_for_status()
            return response.json()["id"]

    async def send_message(self, user: UatUser, session_id: str, content: str) -> httpx.Response:
        async with self.client_for(user) as client:
            return await client.post(
                f"/api/v1/agent/sessions/{session_id}/messages",
                json={"content": content},
                headers={"Idempotency-Key": str(uuid4())},
            )

    async def wait_run_terminal(self, run_id: str, timeout: float = 360.0) -> AgentRun:
        deadline = time.monotonic() + timeout
        # 每轮新建会话：MySQL REPEATABLE READ 长事务复用同一快照，单会话轮询
        # 永远看不到子进程提交的终态。
        while time.monotonic() < deadline:
            async with SessionFactory() as db:
                run = await db.get(AgentRun, run_id)
                if run is not None and run.status in (
                    "completed",
                    "completed_with_warnings",
                    "failed",
                    "cancelled",
                    "clarification_requested",
                ):
                    return run.status
            await asyncio.sleep(0.3)
        raise RuntimeError(f"run_terminal_timeout:{run_id}")

    async def run_by_session(self, session_id: str) -> AgentRun:
        async with SessionFactory() as db:
            run = await db.scalar(
                select(AgentRun)
                .where(AgentRun.session_id == session_id)
                .order_by(AgentRun.created_at.desc())
                .limit(1)
            )
            assert run is not None
            return run

    async def restart_fastapi(self, *, kill_switch: bool) -> None:
        """以新的 kill switch 配置重启 FastAPI 子进程。

        ``PI_GATEWAY_KILL_SWITCH`` 是 FastAPI 进程级启动配置（请求期经
        ``get_settings()`` 读取），运行中无法热切换；测试需要「先建 pi Run、
        再开 kill switch」的时序，只能经进程重启变更。数据库、fake 模型/MCP
        与 gateway 子进程均不受影响（gateway 的 claim 循环持续轮询）。
        """
        if self._fastapi is not None and self._fastapi_pgid is not None:
            await self._stop_process_group(self._fastapi, self._fastapi_pgid)
            self._fastapi_pgid = None
        self.kill_switch = kill_switch
        self._start_fastapi()
        await _wait_http_ok(f"{self.api_base}/healthz")

    async def stop_gateway(self) -> None:
        """提前停掉 gateway 子进程（已 reaped 时 teardown 的重复 SIGTERM 是 no-op）。

        只用于不需要 gateway 参与的用例（如 nonce 重放）：消除 gateway claim
        循环与手工签名请求在 nonce 屏障表上的并发竞争，让断言确定。
        """
        if self._gateway is not None and self._gateway_pgid is not None:
            await self._stop_process_group(self._gateway, self._gateway_pgid)
            self._gateway_pgid = None

    # ------------------------------------------------------------------ #
    # 清理
    # ------------------------------------------------------------------ #

    async def _teardown_db(self) -> None:
        assert_uat_database_scope()
        from app.agent_artifacts.models import (
            AgentArtifact,
            AgentArtifactReadState,
            AgentArtifactVersion,
            ArtifactDraft,
            ArtifactDraftRevision,
            ArtifactEvent,
            ArtifactExport,
            ArtifactPublishAttempt,
            KolDetailCache,
        )
        from app.agent_runtime.models import (
            AgentEvent,
            AgentMessage,
            AgentRunAttempt,
            AgentSession,
            AgentStep,
            AgentToolCall,
            AgentUpload,
            EvidenceItem,
            MemoryEntry,
        )
        from app.billing.models import (
            RuntimeUsageRecord,
            TenantUserQuotaPolicy,
            TenantUserQuotaUsage,
            TenantWallet,
            TenantWalletTransaction,
            Wallet,
            WalletTransaction,
        )
        from app.mcp_gateway.models import McpToolCatalog, McpToolDiscovery
        from app.pi_gateway.models import PiGatewayInstance, PiGatewayRequestNonce, PiTenantQueueState
        from app.runtime_config.models import EncryptedRuntimeSecret, RuntimeConfigVersion

        tenant_ids = self._seeded_tenant_ids
        user_ids = self._seeded_user_ids
        async with SessionFactory.begin() as db:
            await assert_uat_database_connection(db)
            # 测试清理专用：产物/运行/消息之间存在多组互为引用的 FK
            # （versions↔revisions、runs↔messages 等），逐表排序删除极其脆弱；
            # 这里对独立测试库按种子 id 集合整批删除，会话级关闭 FK 检查。
            await db.execute(text("SET FOREIGN_KEY_CHECKS=0"))
            try:
                session_ids = list(
                    (await db.scalars(select(AgentSession.id).where(AgentSession.user_id.in_(user_ids)))).all()
                )
                run_ids = list(
                    (
                        await db.scalars(
                            select(AgentRun.id).where(AgentRun.session_id.in_(session_ids or [""]))
                        )
                    ).all()
                )
                if session_ids:
                    artifact_ids = list(
                        (
                            await db.scalars(
                                select(AgentArtifact.id).where(AgentArtifact.session_id.in_(session_ids))
                            )
                        ).all()
                    )
                    if artifact_ids:
                        version_ids = list(
                            (
                                await db.scalars(
                                    select(AgentArtifactVersion.id).where(
                                        AgentArtifactVersion.artifact_id.in_(artifact_ids)
                                    )
                                )
                            ).all()
                        )
                        if version_ids:
                            await db.execute(
                                delete(ArtifactExport).where(
                                    ArtifactExport.artifact_version_id.in_(version_ids)
                                )
                            )
                        await db.execute(
                            delete(AgentArtifactVersion).where(
                                AgentArtifactVersion.artifact_id.in_(artifact_ids)
                            )
                        )
                        drafts = list(
                            (
                                await db.scalars(
                                    select(ArtifactDraft.id).where(ArtifactDraft.artifact_id.in_(artifact_ids))
                                )
                            ).all()
                        )
                        if drafts:
                            await db.execute(
                                delete(ArtifactDraftRevision).where(ArtifactDraftRevision.draft_id.in_(drafts))
                            )
                            await db.execute(delete(ArtifactDraft).where(ArtifactDraft.id.in_(drafts)))
                        await db.execute(delete(AgentArtifact).where(AgentArtifact.id.in_(artifact_ids)))
                    await db.execute(delete(ArtifactEvent).where(ArtifactEvent.session_id.in_(session_ids)))
                    await db.execute(
                        delete(AgentArtifactReadState).where(
                            AgentArtifactReadState.session_id.in_(session_ids)
                        )
                    )
                    await db.execute(delete(KolDetailCache).where(KolDetailCache.session_id.in_(session_ids)))
                    await db.execute(delete(MemoryEntry).where(MemoryEntry.session_id.in_(session_ids)))
                    await db.execute(delete(AgentUpload).where(AgentUpload.session_id.in_(session_ids)))
                if run_ids:
                    await db.execute(delete(ArtifactPublishAttempt).where(ArtifactPublishAttempt.run_id.in_(run_ids)))
                    await db.execute(
                        delete(ArtifactDraftRevision).where(ArtifactDraftRevision.run_id.in_(run_ids))
                    )
                    await db.execute(delete(RuntimeUsageRecord).where(RuntimeUsageRecord.run_id.in_(run_ids)))
                    await db.execute(delete(AgentToolCall).where(AgentToolCall.run_id.in_(run_ids)))
                    await db.execute(delete(AgentStep).where(AgentStep.run_id.in_(run_ids)))
                    await db.execute(delete(AgentRunAttempt).where(AgentRunAttempt.run_id.in_(run_ids)))
                    await db.execute(delete(AgentEvent).where(AgentEvent.run_id.in_(run_ids)))
                    await db.execute(
                        update(AgentRun)
                        .where(AgentRun.id.in_(run_ids))
                        .values(input_message_id=None)
                    )
                    await db.execute(delete(AgentMessage).where(AgentMessage.run_id.in_(run_ids)))
                    await db.execute(delete(AgentRun).where(AgentRun.id.in_(run_ids)))
                if session_ids:
                    await db.execute(delete(EvidenceItem).where(EvidenceItem.session_id.in_(session_ids)))
                    await db.execute(delete(AgentMessage).where(AgentMessage.session_id.in_(session_ids)))
                    await db.execute(delete(AgentSession).where(AgentSession.id.in_(session_ids)))
                if tenant_ids:
                    await db.execute(delete(RuntimeUsageRecord).where(RuntimeUsageRecord.tenant_id.in_(tenant_ids)))
                    await db.execute(delete(TenantWalletTransaction).where(TenantWalletTransaction.tenant_id.in_(tenant_ids)))
                    await db.execute(delete(TenantUserQuotaUsage).where(TenantUserQuotaUsage.tenant_id.in_(tenant_ids)))
                    await db.execute(delete(TenantUserQuotaPolicy).where(TenantUserQuotaPolicy.tenant_id.in_(tenant_ids)))
                    await db.execute(delete(TenantWallet).where(TenantWallet.tenant_id.in_(tenant_ids)))
                    await db.execute(delete(PiTenantQueueState).where(PiTenantQueueState.tenant_id.in_(tenant_ids)))
                    await db.execute(
                        delete(EncryptedRuntimeSecret).where(EncryptedRuntimeSecret.tenant_id.in_(tenant_ids))
                    )
                    await db.execute(
                        delete(RuntimeConfigVersion).where(RuntimeConfigVersion.tenant_id.in_(tenant_ids))
                    )
                    await db.execute(delete(TenantLicense).where(TenantLicense.tenant_id.in_(tenant_ids)))
                    await db.execute(delete(TenantMembership).where(TenantMembership.tenant_id.in_(tenant_ids)))
                    await db.execute(delete(Tenant).where(Tenant.id.in_(tenant_ids)))
                await db.execute(delete(AuthIdentity).where(AuthIdentity.user_id.in_(user_ids or [""])))
                # legacy 钱包（welcome grant）：0040 升级要求每个 legacy wallet
                # 都有 membership，残留孤儿行会让迁移链 fail-closed。
                await db.execute(delete(WalletTransaction).where(WalletTransaction.user_id.in_(user_ids or [""])))
                await db.execute(delete(Wallet).where(Wallet.user_id.in_(user_ids or [""])))
                await db.execute(delete(User).where(User.id.in_(user_ids or [""])))
                await db.execute(delete(PiGatewayRequestNonce).where(PiGatewayRequestNonce.gateway_id == self.gateway_id))
                await db.execute(delete(PiGatewayInstance).where(PiGatewayInstance.gateway_id == self.gateway_id))
                # 本 UAT 登记/重审的 catalog 与 discovery 行（只限两个 fake 服务）
                fake_tools = (
                    "social_statistic_overview",
                    "social_statistic_trend",
                    "query_analysis_data",
                    "social_statistic_hot_topic",
                    "query_raw_posts",
                    "social_statistic_user_profile",
                    "kol_xiaohongshu_search",
                )
                await db.execute(
                    delete(McpToolCatalog).where(McpToolCatalog.internal_tool_name.in_(fake_tools))
                )
                await db.execute(
                    delete(McpToolDiscovery).where(
                        McpToolDiscovery.service_slug.in_(["insight-cube-mcp", "social-grow-mcp"])
                    )
                )
            finally:
                await db.execute(text("SET FOREIGN_KEY_CHECKS=1"))
