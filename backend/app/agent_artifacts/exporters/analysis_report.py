"""`analysis_report_v1` 到 `workbook_v1` 的导出入口。"""

from __future__ import annotations

from typing import Any

from app.agent_artifacts.payloads.analysis_report import AnalysisReportV1
from app.core.config import get_settings

from .workbook import WorkbookLimits, render_workbook_v1

ANALYSIS_REPORT_EXPORTER_VERSION = "analysis-report-v1.0.0"


def render_analysis_report(payload: dict[str, Any]) -> bytes:
    """使用当前可信配置渲染已发布的 analysis_report_v1。"""
    settings = get_settings()
    return render_workbook_v1(
        AnalysisReportV1.model_validate(payload),
        exporter_version=ANALYSIS_REPORT_EXPORTER_VERSION,
        limits=WorkbookLimits.from_settings(settings),
    )


__all__ = ["ANALYSIS_REPORT_EXPORTER_VERSION", "render_analysis_report"]
