"""已发布 Artifact Excel 导出分派（设计 §12.1 消费边界 / Task 18；v3 加固 A5）。

按 §12.1 消费表，标准 Artifact 与 ``analysis_report_v1`` 支持 Excel 导出；
``kol_analysis_v2``/``kol_detail_v2``/``insight_board_v1``、未发布（无可冻结
payload）的 draft，以及历史/旁路非法 payload（强类型 ValidationError）一律抛
``ArtifactExportUnsupported``，由路由映射为 409，绝不泄漏 500。

导出是表现层能力（§10.1）：只读已发布不可变 Version 的 payload，不调用模型/MCP。
``export_artifact`` 的 ``model``/``gateway`` 关键字参数是保留注入点，导出器
绝不调用它们（测试用桩验证此边界）。
"""

from __future__ import annotations

from pydantic import ValidationError

from app.agent_artifacts.canonical import model_direct_lineage_context
from app.agent_artifacts.exporters.analysis_report import render_analysis_report
from app.agent_artifacts.exporters.brand import render_brand_workbook
from app.agent_artifacts.exporters.campaign import render_campaign_workbook
from app.agent_artifacts.exporters.errors import ArtifactExportUnsupported
from app.agent_artifacts.exporters.kol_selection import render_kol_selection_workbook
from app.agent_artifacts.exporters.workbook import (
    WorkbookLimits,
    WorkbookTechnicalLimitExceeded,
    render_workbook_v1,
    workbook_layout_digest,
)


_SUPPORTED_EXPORTERS = {
    "brand_report_v3": render_brand_workbook,
    "kol_selection_v3": render_kol_selection_workbook,
    "campaign_report_v2": render_campaign_workbook,
    "campaign_report_v3": render_campaign_workbook,
    "analysis_report_v1": render_analysis_report,
}


def export_artifact(version, *, model=None, gateway=None) -> bytes:
    """把已发布不可变 Version 渲染为 .xlsx bytes。

    ``version`` 需暴露 ``schema_version`` 与 ``payload_json``
    （如 ORM ``AgentArtifactVersion``）。数据只来自 ``payload_json``；
    ``model``/``gateway`` 是保留注入点，导出器是纯表现层、绝不调用它们。
    不支持的 schema_version、payload 缺失（draft）或历史/旁路非法 payload
    （强类型 ValidationError）一律抛 ``ArtifactExportUnsupported``
    （→ 409，绝不泄漏 500）。
    """
    del model, gateway  # 表现层边界：永不调用模型/MCP
    schema_version = getattr(version, "schema_version", None)
    version_status = getattr(version, "status", None)
    if version_status is not None and version_status != "published":
        raise ArtifactExportUnsupported(schema_version, reason="version is not published")
    validation_snapshot = getattr(version, "validation_json", None)
    if isinstance(validation_snapshot, dict) and validation_snapshot.get("valid") is False:
        raise ArtifactExportUnsupported(schema_version, reason="version failed publication validity")
    exporter = _SUPPORTED_EXPORTERS.get(schema_version)
    if exporter is None:
        raise ArtifactExportUnsupported(schema_version)
    payload = version.payload_json
    if not isinstance(payload, dict):
        raise ArtifactExportUnsupported(
            schema_version, reason="no published immutable payload"
        )
    # Direct Artifact Skill payload（lineage_snapshot_json.mode == "model_direct_v1"，
    # canonical 无 Evidence）在发布校验时经 direct lineage context 豁免 evidence_ids
    # 要求；导出是同一 payload 的只读渲染，重新校验必须使用同一上下文，否则已发布
    # direct payload 会被误判为 409（提交 3 修复）。legacy Evidence-backed payload 或
    # 缺 lineage_snapshot_json 的历史 Version 不得启用 direct context（Minor #1）。
    lineage_snapshot = getattr(version, "lineage_snapshot_json", None)
    direct_payload = (
        isinstance(lineage_snapshot, dict)
        and lineage_snapshot.get("mode") == "model_direct_v1"
    )
    try:
        if direct_payload:
            with model_direct_lineage_context():
                return exporter(payload)
        return exporter(payload)
    except (ValidationError, ValueError) as exc:
        raise ArtifactExportUnsupported(
            schema_version, reason="published payload fails typed validation"
        ) from exc


__all__ = [
    "ArtifactExportUnsupported",
    "WorkbookLimits",
    "WorkbookTechnicalLimitExceeded",
    "export_artifact",
    "render_analysis_report",
    "render_brand_workbook",
    "render_campaign_workbook",
    "render_kol_selection_workbook",
    "render_workbook_v1",
    "workbook_layout_digest",
]
