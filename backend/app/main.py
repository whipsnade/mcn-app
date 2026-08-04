import os
from collections.abc import Callable, Iterable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.router import api_router
from app.agent_runtime.circuit_breaker import FineGrainedCircuitBreaker
from app.agent_runtime.engine import AgentEngine
from app.agent_runtime.events import AgentEventBroker, AgentEventStream
from app.agent_runtime.executor import AgentRunExecutor
from app.agent_runtime.model_gateway import AgentModelGateway
from app.agent_runtime.recovery import RecoveryLoop
from app.agent_runtime.reviewer import ReviewerDriver
from app.agent_runtime.tools.factory import AgentToolRegistryFactory, resolve_allowlist_entry
from app.agent_runtime.tools.mcp import AgentMcpTool
from app.agent_runtime.utility import UtilityDispatcher, UtilityRunner
from app.core.config import get_settings
from app.db.session import SessionFactory
from app.mcp_gateway.contracts import DataTapService
from app.model.dependencies import get_model_adapter
from app.mcp_gateway.service import get_agent_mcp_transport, refresh_approved_datatap_tools

# 新 Agent 运行时默认租约时长 / 恢复扫描间隔（Task 15）。
AGENT_LEASE_SECONDS = 300
RECOVERY_INTERVAL_SECONDS = 30


def _make_recovery_tool(db, call, *, breaker, transport) -> AgentMcpTool | None:
    """unknown 调用行 → 用于 reconcile 的 AgentMcpTool（只读核对，绝不重放）。

    与 engine 工具共享同一进程级细粒度熔断器与 Agent 传输（设计 §5.3）。
    """
    try:
        service = DataTapService(call.service)
    except ValueError:
        return None
    entry = resolve_allowlist_entry(service, call.internal_tool_name)
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
        transport=transport,
        breaker=breaker,
    )


def create_agent_runtime(*, stuck_seconds: float | None = None) -> tuple[
    AgentRunExecutor, RecoveryLoop, AgentEventBroker, Callable[..., AgentEngine], UtilityDispatcher
]:
    """构建进程级 Agent 执行器 + 恢复循环（共享一个事件 broker）。

    executor 与 recovery 使用**不同**的 worker id（``agent-{pid}`` /
    ``recovery-{pid}``）：若原 worker 的一次 decide 超过租约时长导致租约过期，
    恢复循环接管后原 worker 的 ``renew_lease`` 会因租约归属变化而失败并在下一个
    安全点停止，避免同一 Run 被两个 worker 并发执行（Fix 4）。

    额外返回 ``broker``、``engine_factory`` 与 ``utility_dispatcher``：Task 19
    API 的 SSE 路由共享 broker（同进程事件即时唤醒），kol-details 路由用
    ``engine_factory`` 构建绑定请求会话的 ``AgentEngine`` 驱动
    ``KolDetailRunService``；``utility_dispatcher``（§6.4）是标题/Run 摘要/
    建议的 best-effort 触发器，由 messages 路由与 executor 终态挂接点调用，
    lifespan 启动时 ``start``、关闭时 ``stop``。

    ``engine_factory`` 的 ToolRegistry 由 ``AgentToolRegistryFactory``（设计
    §5.1 生产工具装配唯一入口）构建：注册 history/calculation/artifact 内部
    工具并接入审核 MCP 目录；``channel_permissions`` 由调用方（executor 按 Run
    用户、kol-details 路由按当前用户）查询注入。

    设计 §5.3 接线：进程级共享一个 ``FineGrainedCircuitBreaker``（engine 工具
    与恢复工具同一实例，失败计数跨实例累积）；Agent 路径传输固定
    ``circuit_scope="none"`` + ``retry_policy="never"``（旧服务级熔断与
    possibly-sent 自动重试对新运行时不生效）。
    """
    executor_worker_id = f"agent-{os.getpid()}"
    recovery_worker_id = f"recovery-{os.getpid()}"
    broker = AgentEventBroker()
    breaker = FineGrainedCircuitBreaker()
    agent_transport = get_agent_mcp_transport()
    tool_registry_factory = AgentToolRegistryFactory(
        transport_getter=get_agent_mcp_transport, breaker=breaker
    )

    def engine_factory(
        db: AsyncSession, worker_id: str, channel_permissions: Iterable[str] = ()
    ) -> AgentEngine:
        gateway = AgentModelGateway(get_model_adapter(), db=db)
        registry = tool_registry_factory.build(db)
        return AgentEngine(
            db,
            gateway=gateway,
            registry=registry,
            events=AgentEventStream(db, broker),
            reviewer=ReviewerDriver(db, gateway, worker_id=worker_id),
            worker_id=worker_id,
            channel_permissions=channel_permissions,
            # 租约心跳（§5.5）：独立 Session 真实提交续租，覆盖 decide/MCP/
            # Reviewer 长调用；缺省共享会话只用于测试注入。
            session_factory=SessionFactory,
        )

    def utility_runner_factory(db: AsyncSession) -> UtilityRunner:
        """Utility 内部 Run 执行器（§6.4）：与主 Agent 同一模型端点。"""
        return UtilityRunner(
            db=db,
            gateway=AgentModelGateway(get_model_adapter(), db=db),
            model=get_settings().tencent_plan_model,
        )

    utility_dispatcher = UtilityDispatcher(
        session_factory=SessionFactory, runner_factory=utility_runner_factory
    )

    executor = AgentRunExecutor(
        session_factory=SessionFactory,
        engine_factory=engine_factory,
        worker_id=executor_worker_id,
        lease_seconds=AGENT_LEASE_SECONDS,
        # G1：executor 异常收口补发的 run.failed 经共享 broker 即时送达 SSE 订阅方。
        broker=broker,
        utility_dispatcher=utility_dispatcher,
    )
    recovery = RecoveryLoop(
        executor=executor,
        session_factory=SessionFactory,
        tool_factory=lambda db, call: _make_recovery_tool(
            db, call, breaker=breaker, transport=agent_transport
        ),
        worker_id=recovery_worker_id,
        lease_seconds=AGENT_LEASE_SECONDS,
        interval_seconds=RECOVERY_INTERVAL_SECONDS,
        stuck_seconds=(
            stuck_seconds
            if stuck_seconds is not None
            else get_settings().agent_tool_call_stuck_seconds
        ),
    )
    return executor, recovery, broker, engine_factory, utility_dispatcher


def create_app() -> FastAPI:
    settings = get_settings()
    (
        agent_executor,
        agent_recovery,
        agent_broker,
        agent_engine_factory,
        agent_utility_dispatcher,
    ) = create_agent_runtime()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await refresh_approved_datatap_tools()
        app.state.agent_executor = agent_executor
        app.state.agent_recovery = agent_recovery
        app.state.agent_event_broker = agent_broker
        app.state.agent_engine_factory = agent_engine_factory
        app.state.agent_utility_dispatcher = agent_utility_dispatcher
        app.state.agent_tool_reconciler = get_agent_mcp_transport().reconcile_tool_call
        agent_executor.start()
        agent_recovery.start()
        agent_utility_dispatcher.start()
        try:
            yield
        finally:
            await agent_recovery.stop()
            await agent_executor.stop()
            # 最后停 utility：executor 优雅停机期间收口的 Run 仍能触发，
            # stop 拒绝新触发并等待在途标题/摘要/建议任务完成。
            await agent_utility_dispatcher.stop()

    app = FastAPI(title="KOL Insight API", version="0.1.0", lifespan=lifespan)
    # httpx's ASGI transport may skip lifespan in narrow route tests; keep the
    # same agent runtime available while production startup still performs recovery.
    app.state.agent_executor = agent_executor
    app.state.agent_recovery = agent_recovery
    app.state.agent_event_broker = agent_broker
    app.state.agent_engine_factory = agent_engine_factory
    # 未 start 的 dispatcher schedule 安全空转：窄路由测试不会泄露真实模型调用。
    app.state.agent_utility_dispatcher = agent_utility_dispatcher
    # 管理员人工核对（/admin/agent-tool-calls/{id}/reconcile）取回上游 payload
    # 的只读核对器：与 Agent 调用共享同一传输实例的已确认结果缓存（设计 §5.3）。
    app.state.agent_tool_reconciler = get_agent_mcp_transport().reconcile_tool_call
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
