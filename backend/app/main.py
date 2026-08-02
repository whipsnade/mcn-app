import os
from collections.abc import Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
from app.mcp_gateway.service import (
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


def create_agent_runtime() -> tuple[
    AgentRunExecutor, RecoveryLoop, AgentEventBroker, Callable[[AsyncSession, str], AgentEngine]
]:
    """构建进程级 Agent 执行器 + 恢复循环（共享一个事件 broker）。

    executor 与 recovery 使用**不同**的 worker id（``agent-{pid}`` /
    ``recovery-{pid}``）：若原 worker 的一次 decide 超过租约时长导致租约过期，
    恢复循环接管后原 worker 的 ``renew_lease`` 会因租约归属变化而失败并在下一个
    安全点停止，避免同一 Run 被两个 worker 并发执行（Fix 4）。

    额外返回 ``broker`` 与 ``engine_factory``：Task 19 API 的 SSE 路由共享
    broker（同进程事件即时唤醒），kol-details 路由用 ``engine_factory`` 构建
    绑定请求会话的 ``AgentEngine`` 驱动 ``KolDetailRunService``。
    """
    executor_worker_id = f"agent-{os.getpid()}"
    recovery_worker_id = f"recovery-{os.getpid()}"
    broker = AgentEventBroker()

    def engine_factory(db: AsyncSession, worker_id: str) -> AgentEngine:
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
    return executor, recovery, broker, engine_factory


def create_app() -> FastAPI:
    settings = get_settings()
    agent_executor, agent_recovery, agent_broker, agent_engine_factory = create_agent_runtime()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await refresh_approved_datatap_tools()
        app.state.agent_executor = agent_executor
        app.state.agent_recovery = agent_recovery
        app.state.agent_event_broker = agent_broker
        app.state.agent_engine_factory = agent_engine_factory
        agent_executor.start()
        agent_recovery.start()
        try:
            yield
        finally:
            await agent_recovery.stop()
            await agent_executor.stop()

    app = FastAPI(title="KOL Insight API", version="0.1.0", lifespan=lifespan)
    # httpx's ASGI transport may skip lifespan in narrow route tests; keep the
    # same agent runtime available while production startup still performs recovery.
    app.state.agent_executor = agent_executor
    app.state.agent_recovery = agent_recovery
    app.state.agent_event_broker = agent_broker
    app.state.agent_engine_factory = agent_engine_factory
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
