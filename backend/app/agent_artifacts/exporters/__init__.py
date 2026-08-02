"""已发布 Artifact Excel 导出分派（设计 §12.1 消费边界 / Task 18）。

按 §12.1 消费表，只有 ``brand_report_v3`` 与 ``kol_selection_v3`` 支持首期
Excel 导出；``campaign_report_v2``/``kol_analysis_v2``/``kol_detail_v2``/
``insight_board_v1`` 以及未发布（无可冻结 payload）的 draft 一律抛
``ArtifactExportUnsupported``，由 Task 19 路由映射为 409
``ARTIFACT_EXPORT_UNSUPPORTED``。

导出是表现层能力（§10.1）：只读已发布不可变 Version 的 payload，不调用模型/MCP。
``export_artifact`` 的 ``model``/``gateway`` 关键字参数是保留注入点，导出器
绝不调用它们（测试用桩验证此边界）。
"""

from __future__ import annotations

from app.agent_artifacts.exporters.brand import render_brand_workbook
from app.agent_artifacts.exporters.kol_selection import render_kol_selection_workbook


class ArtifactExportUnsupported(Exception):
    """Artifact 类型不支持 Excel 导出或不是已发布版本（Task 19 → 409）。"""

    code = "ARTIFACT_EXPORT_UNSUPPORTED"

    def __init__(self, schema_version: str, *, reason: str = "") -> None:
        self.schema_version = schema_version
        self.reason = reason
        detail = f"artifact type {schema_version!r} is not supported for Excel export"
        if reason:
            detail = f"{detail}: {reason}"
        super().__init__(detail)


_SUPPORTED_EXPORTERS = {
    "brand_report_v3": render_brand_workbook,
    "kol_selection_v3": render_kol_selection_workbook,
}


def export_artifact(version, *, model=None, gateway=None) -> bytes:
    """把已发布不可变 Version 渲染为 .xlsx bytes。

    ``version`` 需暴露 ``schema_version`` 与 ``payload_json``
    （如 ORM ``AgentArtifactVersion``）。数据只来自 ``payload_json``；
    ``model``/``gateway`` 是保留注入点，导出器是纯表现层、绝不调用它们。
    不支持的 schema_version 或 payload 缺失（draft）抛 ``ArtifactExportUnsupported``。
    """
    del model, gateway  # 表现层边界：永不调用模型/MCP
    schema_version = getattr(version, "schema_version", None)
    exporter = _SUPPORTED_EXPORTERS.get(schema_version)
    if exporter is None:
        raise ArtifactExportUnsupported(schema_version)
    payload = version.payload_json
    if not isinstance(payload, dict):
        raise ArtifactExportUnsupported(
            schema_version, reason="no published immutable payload"
        )
    return exporter(payload)


__all__ = [
    "ArtifactExportUnsupported",
    "export_artifact",
    "render_brand_workbook",
    "render_kol_selection_workbook",
]
