"""生产工具装配与用户能力过滤的唯一入口（设计 §5.1）。

:class:`AgentToolRegistryFactory` 为每个 Engine 构建完整的 :class:`ToolRegistry`：

- history：``read_artifact`` / ``search_evidence`` / ``read_tool_result`` /
  ``remember_scope``（confirmed_scope 范围记忆写入）；
- calculation：``calculate_expression`` / ``aggregate_metrics`` /
  ``calculate_period_comparison`` / ``normalize_sentiment`` / ``rank_kols``；
- artifact：``create_draft`` / ``update_draft`` / ``abandon_draft`` 与六个
  Builder 工具（``build_brand_report_draft`` / ``build_campaign_report_draft`` /
  ``build_kol_selection_draft`` / ``build_kol_analysis_draft`` /
  ``build_kol_detail_draft`` / ``build_insight_draft``，v3 加固 §6.1 + H5）；
- MCP：目录中当前仍 approved、enabled 且签名未变的工具，且仅审核 allowlist
  （``DYNAMIC_TOOL_ALLOWLIST``）内的目录行才挂执行器（UAT 发现：实时网关以
  审核内部名暴露工具，remote_name 一律取内部名，见 main.py 原注释 /
  2026-08-02-agent-runtime-uat.md Incident）。

工具执行前的实时目录复核（G2，修复设计 §5.1）：``build`` 注入
``catalog_lookup``，``execute`` 每次 dispatch MCP 工具前按内部名单行查询
目录（存在 / approved / enabled / 签名 digest 与装配时一致），复核在积分
预留之前失败即拒绝；``visible_tools`` 语义不变，仍用装配时缓存。

:func:`load_channel_permissions` 是用户渠道权限的唯一查询入口：Engine 创建时
按 ``user_id`` 注入；默认空权限只能隐藏受限工具，不能作为生产用户的永久配置。
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.profiles import ARTIFACT_TOOLS, CALCULATION_TOOLS, HISTORY_TOOLS
from app.agent_runtime.circuit_breaker import FineGrainedCircuitBreaker
from app.agent_runtime.tools.artifacts import AbandonDraftTool, CreateDraftTool, UpdateDraftTool
from app.agent_runtime.tools.builders import (
    BuildBrandReportDraftTool,
    BuildCampaignReportDraftTool,
    BuildInsightDraftTool,
    BuildKolAnalysisDraftTool,
    BuildKolDetailDraftTool,
    BuildKolSelectionDraftTool,
)
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
    RememberScopeTool,
    SearchEvidenceTool,
)
from app.agent_runtime.tools.mcp import AgentMcpTool, SessionFactoryLike
from app.agent_runtime.tools.registry import CatalogRow, McpCatalogEntry, ToolRegistry
from app.db.session import SessionFactory
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
    ``session_factory`` 供执行前实时复核目录行使用：独立会话短事务，读到
    的是最新已提交状态（不复用 Engine 会话，避免 REPEATABLE READ 快照下
    看不到装配后管理员提交的撤销/隔离/禁用）；测试可注入共享会话。
    """

    def __init__(
        self,
        *,
        transport_getter: Callable[[], McpTransport] = get_agent_mcp_transport,
        breaker: FineGrainedCircuitBreaker | None = None,
        session_factory: SessionFactoryLike = SessionFactory,
    ) -> None:
        self._transport_getter = transport_getter
        self._breaker = breaker
        self._session_factory = session_factory

    def build(self, db: AsyncSession) -> ToolRegistry:
        """构建注册齐内部工具并接入 MCP 审核目录的 ToolRegistry。"""
        registry = ToolRegistry(
            catalog_source=lambda: self._load_catalog(db),
            mcp_executor_factory=lambda row: self._make_mcp_tool(db, row),
            catalog_lookup=self._lookup_catalog_row,
        )
        registry.register(ReadArtifactTool(db), category=HISTORY_TOOLS)
        registry.register(SearchEvidenceTool(db), category=HISTORY_TOOLS)
        registry.register(ReadToolResultTool(db), category=HISTORY_TOOLS)
        registry.register(RememberScopeTool(db), category=HISTORY_TOOLS)
        registry.register(CalculateExpressionTool(db), category=CALCULATION_TOOLS)
        registry.register(AggregateMetricsTool(db), category=CALCULATION_TOOLS)
        registry.register(CalculatePeriodComparisonTool(db), category=CALCULATION_TOOLS)
        registry.register(NormalizeSentimentTool(db), category=CALCULATION_TOOLS)
        registry.register(RankKolsTool(db), category=CALCULATION_TOOLS)
        registry.register(CreateDraftTool(db), category=ARTIFACT_TOOLS)
        registry.register(UpdateDraftTool(db), category=ARTIFACT_TOOLS)
        registry.register(AbandonDraftTool(db), category=ARTIFACT_TOOLS)
        # §6.1 Builder 工具：Evidence → 正式 Artifact 的确定性转换，与 Draft
        # 创建/更新同属 artifact 分类（信任层级与可见 Profile 完全一致，不另设
        # BUILDER_TOOLS 词汇）。
        registry.register(BuildBrandReportDraftTool(db), category=ARTIFACT_TOOLS)
        registry.register(BuildCampaignReportDraftTool(db), category=ARTIFACT_TOOLS)
        registry.register(BuildKolSelectionDraftTool(db), category=ARTIFACT_TOOLS)
        registry.register(BuildKolAnalysisDraftTool(db), category=ARTIFACT_TOOLS)
        registry.register(BuildKolDetailDraftTool(db), category=ARTIFACT_TOOLS)
        registry.register(BuildInsightDraftTool(db), category=ARTIFACT_TOOLS)
        return registry

    async def _load_catalog(self, db: AsyncSession):
        return (await db.scalars(select(McpToolCatalog))).all()

    async def _lookup_catalog_row(self, internal_tool_name: str) -> McpCatalogEntry | None:
        """按内部名实时查询目录行（G2）：独立会话单行查询，返回内存快照。

        快照在会话内取出，避免 ORM 行脱离会话后的懒加载问题。
        """
        async with self._session_factory() as db:
            row = await db.scalar(
                select(McpToolCatalog).where(
                    McpToolCatalog.internal_tool_name == internal_tool_name
                )
            )
            if row is None:
                return None
            return McpCatalogEntry(
                internal_tool_name=row.internal_tool_name,
                service_slug=row.service_slug,
                reviewed_description=row.reviewed_description,
                input_schema_json=row.input_schema_json,
                review_status=row.review_status,
                is_enabled=row.is_enabled,
                discovery_digest=row.discovery_digest,
            )

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
