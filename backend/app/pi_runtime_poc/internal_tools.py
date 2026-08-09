"""Pi POC 内部工具桥：只暴露受控历史、Builder 与确定性发布工具。

Pi 可见目录固定为白名单（见 :data:`PI_POC_ALLOWED_TOOLS`）：历史读取
（search_evidence / read_tool_result / read_artifact）、六类强类型 Builder、
get_session_context 与 publish_artifacts。禁止 bash/shell/文件编辑/任意 HTTP/
Draft 直写（create_draft/update_draft/abandon_draft）/计算/记忆等越权工具；
DataTap 由 Task 4 Extension 直连，本 Registry 不注册 ``AgentMcpTool``。
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.profiles import (
    ARTIFACT_TOOLS,
    HISTORY_TOOLS,
    AgentProfile,
)
from app.agent_runtime.tools.pi_internal_tools import (
    GetSessionContextTool,
    LoadMarketingSkillTool,
    PublishArtifactsTool,
    RequestClarificationTool,
)
from app.agent_runtime.tools.builders import (
    BuildBrandReportDraftTool,
    BuildCampaignReportDraftTool,
    BuildInsightDraftTool,
    BuildKolAnalysisDraftTool,
    BuildKolDetailDraftTool,
    BuildKolSelectionDraftTool,
)
from app.agent_runtime.tools.history import (
    ReadArtifactTool,
    ReadToolResultTool,
    SearchEvidenceTool,
)
from app.agent_runtime.tools.registry import ToolRegistry

# Pi 可见内部工具白名单（设计 §方案 A Task 5；与前端 internal-tools.ts 镜像）。
PI_POC_ALLOWED_TOOLS: frozenset[str] = frozenset(
    {
        "get_session_context",
        "load_marketing_skill",
        "search_evidence",
        "read_tool_result",
        "read_artifact",
        "build_brand_report_draft",
        "build_campaign_report_draft",
        "build_kol_selection_draft",
        "build_kol_analysis_draft",
        "build_kol_detail_draft",
        "build_insight_draft",
        "publish_artifacts",
        "request_clarification",
    }
)

# POC Profile：只放行 history 与 artifact 分类；无 MCP/calculation。
PIPOC_PROFILE = AgentProfile(
    name="pi_poc",
    version="v1",
    allowed_actions=frozenset(),
    allowed_tool_categories=frozenset({HISTORY_TOOLS, ARTIFACT_TOOLS}),
    requires_reviewer=False,
    max_context_budget=0,
    output_schema="agent_actions",
    system_prompt_key="pi_poc_v1",
)


def build_pi_internal_registry(*, db: AsyncSession, worker_id: str) -> ToolRegistry:
    """构建只含白名单工具的 Registry；不注册任何 MCP/计算/Draft 直写工具。"""
    registry = ToolRegistry()
    registry.register(ReadArtifactTool(db), category=HISTORY_TOOLS)
    registry.register(SearchEvidenceTool(db), category=HISTORY_TOOLS)
    registry.register(ReadToolResultTool(db), category=HISTORY_TOOLS)
    registry.register(GetSessionContextTool(db), category=HISTORY_TOOLS)
    registry.register(LoadMarketingSkillTool(db), category=HISTORY_TOOLS)
    registry.register(BuildBrandReportDraftTool(db), category=ARTIFACT_TOOLS)
    registry.register(BuildCampaignReportDraftTool(db), category=ARTIFACT_TOOLS)
    registry.register(BuildKolSelectionDraftTool(db), category=ARTIFACT_TOOLS)
    registry.register(BuildKolAnalysisDraftTool(db), category=ARTIFACT_TOOLS)
    registry.register(BuildKolDetailDraftTool(db), category=ARTIFACT_TOOLS)
    registry.register(BuildInsightDraftTool(db), category=ARTIFACT_TOOLS)
    registry.register(PublishArtifactsTool(db, worker_id=worker_id), category=ARTIFACT_TOOLS)
    registry.register(RequestClarificationTool(db, worker_id=worker_id), category=HISTORY_TOOLS)
    return registry


__all__ = [
    "PIPOC_PROFILE",
    "PI_POC_ALLOWED_TOOLS",
    "GetSessionContextTool",
    "LoadMarketingSkillTool",
    "PublishArtifactsTool",
    "RequestClarificationTool",
    "build_pi_internal_registry",
]
