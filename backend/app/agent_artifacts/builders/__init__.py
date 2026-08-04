"""强类型 Artifact Draft builders（设计 §12.1 / Task 16 / v3 加固 §6.1 B2）。

builders 只做「转换」：把模型已选定的 Evidence + 确定性计算结果转换为强类型
Draft payload（brand_report_v3 / campaign_report_v2 / kol_selection_v3 /
kol_analysis_v2 / kol_detail_v2）。它们绝不决定要调用哪些 MCP 工具、不发起
外部查询、不改变用户目标——那是引擎里模型的职责（§3.3 红线）。
"""

from app.agent_artifacts.builders.brand import build_brand_report_draft
from app.agent_artifacts.builders.campaign import build_campaign_report_draft
from app.agent_artifacts.builders.common import (
    DraftBuildError,
    DraftBuildResult,
    LineageCollector,
)
from app.agent_artifacts.builders.kol_analysis import build_kol_analysis_draft
from app.agent_artifacts.builders.kol_detail import build_kol_detail_draft
from app.agent_artifacts.builders.kol_selection import build_kol_selection_draft

__all__ = [
    "DraftBuildError",
    "DraftBuildResult",
    "LineageCollector",
    "build_brand_report_draft",
    "build_campaign_report_draft",
    "build_kol_analysis_draft",
    "build_kol_detail_draft",
    "build_kol_selection_draft",
]
