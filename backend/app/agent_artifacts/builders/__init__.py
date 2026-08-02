"""强类型 Artifact Draft builders（设计 §12.1 / Task 16）。

builders 只做「转换」：把模型已选定的 Evidence + 确定性计算结果转换为强类型
``kol_selection_v3`` / ``kol_analysis_v2`` Draft payload。它们绝不决定要调用哪些
MCP 工具——那是引擎里模型的职责（Task 14）。
"""

from app.agent_artifacts.builders.common import DraftBuildError, DraftBuildResult
from app.agent_artifacts.builders.kol_analysis import build_kol_analysis_draft
from app.agent_artifacts.builders.kol_detail import build_kol_detail_draft
from app.agent_artifacts.builders.kol_selection import build_kol_selection_draft

__all__ = [
    "DraftBuildError",
    "DraftBuildResult",
    "build_kol_analysis_draft",
    "build_kol_detail_draft",
    "build_kol_selection_draft",
]
