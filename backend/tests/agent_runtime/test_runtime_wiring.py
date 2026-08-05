"""运行时生产装配接线测试（v3 加固设计 §5.3）。

- ``create_agent_runtime`` 的 ``engine_factory`` 为所有 MCP 工具注入**同一进程级**
  细粒度熔断器（不再每工具实例新建空熔断器）；
- Agent 路径 transport 固定 ``circuit_scope="none"`` + ``retry_policy="never"``
  （旧服务级熔断与 possibly-sent 自动重试对新运行时不生效）；
- 恢复工具工厂与 engine 工具共享同一熔断器与 Agent 传输；
- ``app.state.agent_tool_reconciler`` 已接线到 Agent 传输的 ``reconcile_tool_call``
  （admin confirm_success 才能取回 payload 建 Evidence）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from app.agent_runtime.profiles import PROFILES
from app.mcp_gateway.models import McpToolCatalog
from app.mcp_gateway.service import get_agent_mcp_transport

session_analyst = PROFILES["session_analyst_v1"]


async def _add_approved_catalog_row(db_session, *, internal_name: str) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    db_session.add(
        McpToolCatalog(
            id=str(uuid4()),
            service_slug="insight-cube-mcp",
            internal_tool_name=internal_name,
            reviewed_description=f"{internal_name} 描述",
            input_schema_json={"type": "object"},
            output_validator_version="v1",
            discovery_digest="d" * 64,
            review_status="approved",
            is_enabled=True,
            created_at=now,
            updated_at=now,
        )
    )
    await db_session.flush()


async def _engine_mcp_tool(engine, internal_name: str):
    registry = engine._registry
    await registry.visible_tools(session_analyst)  # 触发目录加载与执行器装配
    return registry._entries[internal_name].tool


async def test_engine_factory_shares_one_breaker_and_agent_transport(db_session) -> None:
    from app.main import create_agent_runtime

    await _add_approved_catalog_row(db_session, internal_name="query_analysis_data")
    _executor, _recovery, _broker, engine_factory, _utility = create_agent_runtime()
    engine_a = engine_factory(db_session, "worker-a", channel_permissions=())
    engine_b = engine_factory(db_session, "worker-b", channel_permissions=())

    tool_a = await _engine_mcp_tool(engine_a, "query_analysis_data")
    tool_b = await _engine_mcp_tool(engine_b, "query_analysis_data")

    # 进程级共享熔断器：跨 engine、跨工具实例是同一实例（失败计数才能累积）
    assert tool_a._breaker is tool_b._breaker
    # Agent 传输：旧服务级熔断关闭（由细粒度熔断单独负责），possibly-sent 禁止自动重试
    assert tool_a._transport is get_agent_mcp_transport()
    assert tool_a._transport.circuit_scope == "none"
    assert tool_a._transport.retry_policy == "never"


async def test_recovery_tool_factory_shares_breaker_and_agent_transport(db_session) -> None:
    from app.main import create_agent_runtime

    await _add_approved_catalog_row(db_session, internal_name="query_analysis_data")
    _executor, recovery, _broker, engine_factory, _utility = create_agent_runtime()
    engine = engine_factory(db_session, "worker-a", channel_permissions=())
    engine_tool = await _engine_mcp_tool(engine, "query_analysis_data")

    stub_call = SimpleNamespace(
        service="insight-cube-mcp", internal_tool_name="query_analysis_data"
    )
    recovery_tool = recovery._tool_factory(db_session, stub_call)

    assert recovery_tool is not None
    assert recovery_tool._breaker is engine_tool._breaker
    assert recovery_tool._transport is engine_tool._transport


async def test_engine_factory_does_not_wire_model_reviewer(db_session) -> None:
    """直接发布改造（Task 4）：engine_factory 不再构造 ReviewerDriver——

    新执行路径不创建 Reviewer Run、不进入 reviewing；发布由确定性
    ``ArtifactPublicationService`` 完成。``artifact_reviewer_v1`` Profile
    注册仅保留供历史代码导入，不得出现在新 Runtime wiring。
    """
    from app.main import create_agent_runtime

    _executor, _recovery, _broker, engine_factory, _utility = create_agent_runtime()
    engine = engine_factory(db_session, "worker-a", channel_permissions=())

    assert not hasattr(engine, "_reviewer")


def test_app_state_wires_agent_tool_reconciler() -> None:
    from app.main import create_app

    app = create_app()

    reconciler = getattr(app.state, "agent_tool_reconciler", None)
    assert reconciler is not None
    # 接线到 Agent 传输的只读核对方法（与 Agent 调用共享 _recent_results 缓存）
    assert reconciler == get_agent_mcp_transport().reconcile_tool_call


def test_agent_transport_enables_wall_clock_timeout() -> None:
    """cutover 阻断项 1：Agent 传输启用外发墙钟上限；legacy 传输保持缺省（不启用）。"""
    from app.core.config import get_settings
    from app.mcp_gateway.service import get_mcp_transport

    agent = get_agent_mcp_transport()
    assert agent._call_timeout_seconds == get_settings().agent_mcp_call_timeout_seconds
    assert get_mcp_transport()._call_timeout_seconds is None
