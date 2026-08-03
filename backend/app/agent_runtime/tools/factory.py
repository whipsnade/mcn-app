"""生产工具装配与用户能力过滤的唯一入口（设计 §5.1）。

:class:`AgentToolRegistryFactory` 为每个 Engine 构建完整的 :class:`ToolRegistry`：

- history：``read_artifact`` / ``search_evidence`` / ``read_tool_result``；
- calculation：``calculate_expression`` / ``aggregate_metrics`` /
  ``calculate_period_comparison`` / ``normalize_sentiment`` / ``rank_kols``；
- artifact：``create_draft`` / ``update_draft``；
- MCP：目录中当前仍 approved、enabled 且签名未变的工具，且仅审核 allowlist
  （``DYNAMIC_TOOL_ALLOWLIST``）内的目录行才挂执行器（UAT 发现：实时网关以
  审核内部名暴露工具，remote_name 一律取内部名，见 main.py 原注释 /
  2026-08-02-agent-runtime-uat.md Incident）。

工具执行前的实时目录复核保持 :class:`ToolRegistry` 既有语义
（``visible_tools`` / ``execute`` 复查 review_status/is_enabled）。

:func:`load_channel_permissions` 是用户渠道权限的唯一查询入口：Engine 创建时
按 ``user_id`` 注入；默认空权限只能隐藏受限工具，不能作为生产用户的永久配置。
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.profiles import ARTIFACT_TOOLS, CALCULATION_TOOLS, HISTORY_TOOLS
from app.agent_runtime.circuit_breaker import FineGrainedCircuitBreaker
from app.agent_runtime.tools.artifacts import CreateDraftTool, UpdateDraftTool
from app.agent_runtime.tools.calculation import (
    AggregateMetricsTool,
    CalculateExpressionTool,
    CalculatePeriodComparisonTool,
    NormalizeSentimentTool,
    RankKolsTool,
)
from app.agent_runtime.tools.history import (
    ReadArtifactTool,
    ReadToolResultTool,
    SearchEvidenceTool,
)
from app.agent_runtime.tools.mcp import AgentMcpTool
from app.agent_runtime.tools.registry import CatalogRow, ToolRegistry
from app.identity.models import UserChannelPermission
from app.mcp_gateway.contracts import DataTapService
from app.mcp_gateway.models import McpToolCatalog
from app.mcp_gateway.registry import DYNAMIC_TOOL_ALLOWLIST
from app.mcp_gateway.service import get_agent_mcp_transport
from app.mcp_gateway.transport import McpTransport


def resolve_allowlist_entry(
    service: DataTapService, internal_tool_name: str
) -> tuple[str, str, dict] | None:
    """从审核 allowlist 解析 (remote_name, description, output_schema)；未收录返回 None。

    remote_name 一律取内部名，与实时 DataTap 网关暴露的工具名保持一致。
    """
    entry = DYNAMIC_TOOL_ALLOWLIST.get(service, {}).get(internal_tool_name)
    if entry is None:
        return None
    _stale_remote_name, description, output_schema = entry
    return (internal_tool_name, description, output_schema)


async def load_channel_permissions(db: AsyncSession, user_id: str) -> frozenset[str]:
    """查询用户启用中的渠道权限集合（``user_channel_permissions``）。"""
    rows = await db.scalars(
        select(UserChannelPermission.channel).where(
            UserChannelPermission.user_id == user_id,
            UserChannelPermission.is_enabled.is_(True),
        )
    )
    return frozenset(rows.all())


class AgentToolRegistryFactory:
    """生产工具装配工厂：给定 db session 构建完整 ToolRegistry。

    ``transport_getter`` 可注入以便测试替换；默认取 Agent 运行时专用传输
    （``circuit_scope="none"`` + 禁止 possibly-sent 自动重试，设计 §5.3）。
    ``breaker`` 为进程级共享细粒度熔断器，生产必须由 main 装配注入；为 None
    时每个 MCP 工具实例各建独立熔断器（仅测试便利，失败计数不跨实例累积）。
    """

    def __init__(
        self,
        *,
        transport_getter: Callable[[], McpTransport] = get_agent_mcp_transport,
        breaker: FineGrainedCircuitBreaker | None = None,
    ) -> None:
        self._transport_getter = transport_getter
        self._breaker = breaker

    def build(self, db: AsyncSession) -> ToolRegistry:
        """构建注册齐内部工具并接入 MCP 审核目录的 ToolRegistry。"""
        registry = ToolRegistry(
            catalog_source=lambda: self._load_catalog(db),
            mcp_executor_factory=lambda row: self._make_mcp_tool(db, row),
        )
        registry.register(ReadArtifactTool(db), category=HISTORY_TOOLS)
        registry.register(SearchEvidenceTool(db), category=HISTORY_TOOLS)
        registry.register(ReadToolResultTool(db), category=HISTORY_TOOLS)
        registry.register(CalculateExpressionTool(db), category=CALCULATION_TOOLS)
        registry.register(AggregateMetricsTool(db), category=CALCULATION_TOOLS)
        registry.register(CalculatePeriodComparisonTool(db), category=CALCULATION_TOOLS)
        registry.register(NormalizeSentimentTool(db), category=CALCULATION_TOOLS)
        registry.register(RankKolsTool(db), category=CALCULATION_TOOLS)
        registry.register(CreateDraftTool(db), category=ARTIFACT_TOOLS)
        registry.register(UpdateDraftTool(db), category=ARTIFACT_TOOLS)
        return registry

    async def _load_catalog(self, db: AsyncSession):
        return (await db.scalars(select(McpToolCatalog))).all()

    def _make_mcp_tool(self, db: AsyncSession, row: CatalogRow) -> AgentMcpTool | None:
        """目录行 → AgentMcpTool（未在 allowlist 内不挂执行器，工具不可调用）。"""
        try:
            service = DataTapService(row.service_slug)
        except ValueError:
            return None
        entry = resolve_allowlist_entry(service, row.internal_tool_name)
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
            transport=self._transport_getter(),
            breaker=self._breaker,
        )


__all__ = [
    "AgentToolRegistryFactory",
    "load_channel_permissions",
    "resolve_allowlist_entry",
]
