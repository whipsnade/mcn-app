"""Exporter errors shared by the dispatcher and individual renderers."""

from __future__ import annotations


class ArtifactExportUnsupported(Exception):
    """Artifact 类型不支持 Excel 导出或不是已发布版本（→ 409）。"""

    code = "ARTIFACT_EXPORT_UNSUPPORTED"

    def __init__(self, schema_version: str, *, reason: str = "") -> None:
        self.schema_version = schema_version
        self.reason = reason
        detail = f"artifact type {schema_version!r} is not supported for Excel export"
        if reason:
            detail = f"{detail}: {reason}"
        super().__init__(detail)
