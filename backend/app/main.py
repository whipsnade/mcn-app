import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from app.api.router import api_router
from app.agent_runtime.engine import AgentEngine
from app.agent_runtime.events import AgentEventBroker, AgentEventStream
from app.agent_runtime.executor import AgentRunExecutor
from app.agent_runtime.model_gateway import AgentModelGateway
from app.agent_runtime.recovery import RecoveryLoop
from app.agent_runtime.reviewer import ReviewerDriver
from app.agent_runtime.tools.mcp import AgentMcpTool
from app.agent_runtime.tools.registry import ToolRegistry
from app.core.config import get_settings
from app.db.session import SessionFactory
from app.mcp_gateway.contracts import DataTapService
from app.mcp_gateway.models import McpToolCatalog
from app.mcp_gateway.registry import DYNAMIC_TOOL_ALLOWLIST
from app.model.dependencies import get_model_adapter
from app.quick.service import release_stale_quick_calls
from app.tasks.dependencies import (
    create_task_runtime,
    get_mcp_transport,
    refresh_approved_datatap_tools,
)

# 新 Agent 运行时默认租约时长 / 恢复扫描间隔（Task 15）。
AGENT_LEASE_SECONDS = 300
RECOVERY_INTERVAL_SECONDS = 30


def _resolve_remote_entry(service: DataTapService, internal_tool_name: str):
    """从审核 allowlist 解析 (remote_name, description, output_schema)；未收录返回 None。"""
    return DYNAMIC_TOOL_ALLOWLIST.get(service, {}).get(internal_tool_name)


async def _load_catalog(db):
    return (await db.scalars(select(McpToolCatalog))).all()


def _make_mcp_tool(db, row) -> AgentMcpTool | None:
    """目录行 → AgentMcpTool（未在 allowlist 内不挂执行器，工具不可调用）。"""
    try:
        service = DataTapService(row.service_slug)
    except ValueError:
        return None
    entry = _resolve_remote_entry(service, row.internal_tool_name)
    if entry is None:
        return None
    remote_name, _description, output_schema = entry
    return AgentMcpTool(
        internal_name=row.internal_tool_name,
        service=service,
        remote_name=remote_name,
        input_schema=row.input_schema_json,
        output_schema=output_schema,
        db_session=db,
        transport=get_mcp_transport(),
    )


def _make_recovery_tool(db, call) -> AgentMcpTool | None:
    """unknown 调用行 → 用于 reconcile 的 AgentMcpTool（只读核对，绝不重放）。"""
    try:
        service = DataTapService(call.service)
    except ValueError:
        return None
    entry = _resolve_remote_entry(service, call.internal_tool_name)
    if entry is None:
        return None
    remote_name, _description, output_schema = entry
    return AgentMcpTool(
        internal_name=call.internal_tool_name,
        service=service,
        remote_name=remote_name,
        input_schema={},
        output_schema=output_schema,
        db_session=db,
        transport=get_mcp_transport(),
    )


def create_agent_runtime() -> tuple[AgentRunExecutor, RecoveryLoop]:
    """构建进程级 Agent 执行器 + 恢复循环（共享一个事件 broker）。

    executor 与 recovery 使用**不同**的 worker id（``agent-{pid}`` /
    ``recovery-{pid}``）：若原 worker 的一次 decide 超过租约时长导致租约过期，
    恢复循环接管后原 worker 的 ``renew_lease`` 会因租约归属变化而失败并在下一个
    安全点停止，避免同一 Run 被两个 worker 并发执行（Fix 4）。
    """
    executor_worker_id = f"agent-{os.getpid()}"
    recovery_worker_id = f"recovery-{os.getpid()}"
    broker = AgentEventBroker()

    def engine_factory(db, worker_id) -> AgentEngine:
        gateway = AgentModelGateway(get_model_adapter(), db=db)
        registry = ToolRegistry(
            catalog_source=lambda: _load_catalog(db),
            mcp_executor_factory=lambda row: _make_mcp_tool(db, row),
        )
        return AgentEngine(
            db,
            gateway=gateway,
            registry=registry,
            events=AgentEventStream(db, broker),
            reviewer=ReviewerDriver(db, gateway, worker_id=worker_id),
            worker_id=worker_id,
        )

    executor = AgentRunExecutor(
        session_factory=SessionFactory,
        engine_factory=engine_factory,
        worker_id=executor_worker_id,
        lease_seconds=AGENT_LEASE_SECONDS,
    )
    recovery = RecoveryLoop(
        executor=executor,
        session_factory=SessionFactory,
        tool_factory=_make_recovery_tool,
        worker_id=recovery_worker_id,
        lease_seconds=AGENT_LEASE_SECONDS,
        interval_seconds=RECOVERY_INTERVAL_SECONDS,
    )
    return executor, recovery


def create_app() -> FastAPI:
    settings = get_settings()
    runner, recovery = create_task_runtime()
    agent_executor, agent_recovery = create_agent_runtime()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await refresh_approved_datatap_tools()
        app.state.task_runner = runner
        app.state.agent_executor = agent_executor
        app.state.agent_recovery = agent_recovery
        agent_executor.start()
        agent_recovery.start()
        stop_recovery = asyncio.Event()

        async def recover_once() -> None:
            try:
                await recovery.recover_expired()
                await recovery.recover_pending_followups()
                await release_stale_quick_calls(older_than_seconds=300)
            except Exception:
                # A later fixed-interval pass retries transient database faults.
                return

        async def recover_periodically() -> None:
            while not stop_recovery.is_set():
                try:
                    await asyncio.wait_for(stop_recovery.wait(), timeout=30)
                except TimeoutError:
                    await recover_once()

        startup_recovery = asyncio.create_task(recover_once())
        coordinator = asyncio.create_task(recover_periodically())
        try:
            yield
        finally:
            stop_recovery.set()
            await coordinator
            await startup_recovery
            await runner.shutdown()
            await agent_recovery.stop()
            await agent_executor.stop()

    app = FastAPI(title="KOL Insight API", version="0.1.0", lifespan=lifespan)
    # httpx's ASGI transport may skip lifespan in narrow route tests; keep the
    # same runner available while production startup still performs recovery.
    app.state.task_runner = runner
    app.state.agent_executor = agent_executor
    app.state.agent_recovery = agent_recovery
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_origin],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "kol-insight-api"}

    app.include_router(api_router)
    return app


app = create_app()
