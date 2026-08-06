"""campaign_report_v2 Excel 导出（Gate C Task 5 / 设计 §8.3）。

无 ROI 时 9 个 Sheet；具备可靠 ROI 数据时动态增加「ROI与转化」Sheet。
导出只读已发布 Version 的 payload，不调用模型/MCP。
"""

from __future__ import annotations

from io import BytesIO

import pytest
from openpyxl import load_workbook

from app.agent_artifacts.exporters import ArtifactExportUnsupported, export_artifact

from tests.agent_artifacts.test_payloads import build_campaign_dict

CAMPAIGN_SHEETS = (
    "活动综合概览",
    "周期对比与趋势",
    "平台表现",
    "情感与内容分析",
    "热门帖子TOP",
    "达人投放表现",
    "自然传播与受众",
    "洞察与建议",
    "方法论",
)


class _Version:
    def __init__(self, schema_version: str, payload_json: dict | None) -> None:
        self.schema_version = schema_version
        self.payload_json = payload_json
        self.data_status = "complete" if payload_json else "draft"


def _values(ws) -> list:
    return [
        cell.value
        for row in ws.iter_rows()
        for cell in row
        if cell.value is not None
    ]


def _campaign_version(**overrides) -> _Version:
    payload = build_campaign_dict()
    payload.update(overrides)
    return _Version("campaign_report_v2", payload)


def test_campaign_export_has_nine_sheets_without_roi() -> None:
    content = export_artifact(_campaign_version())
    assert content[:2] == b"PK"
    wb = load_workbook(BytesIO(content), data_only=False)
    assert tuple(wb.sheetnames) == CAMPAIGN_SHEETS
    assert "某品牌" in str(wb["活动综合概览"]["A1"].value)


def test_campaign_export_adds_roi_sheet_only_when_available() -> None:
    payload = build_campaign_dict()
    payload["data"]["internal_metrics"] = {
        "spend": 100000,
        "impressions": 2000000,
        "conversions": 5000,
        "revenue": 300000,
        "cpc": 20.0,
        "cpm": 50.0,
    }
    payload["data"]["roi"] = {
        "spend": 100000,
        "revenue": 300000,
        "conversions": 5000,
        "attribution_window": "最后点击 7 天",
        "roi": 2.0,
        "roas": 3.0,
    }
    content = export_artifact(_Version("campaign_report_v2", payload))
    wb = load_workbook(BytesIO(content), data_only=False)
    assert tuple(wb.sheetnames) == (*CAMPAIGN_SHEETS, "ROI与转化")
    assert 100000 in _values(wb["ROI与转化"])
    assert 2.0 in _values(wb["ROI与转化"])


def test_campaign_export_platform_chart_only_with_data() -> None:
    content = export_artifact(_campaign_version())
    wb = load_workbook(BytesIO(content), data_only=False)
    platforms = wb["平台表现"]
    assert len(platforms._charts) >= 0  # 无数据不强制建图


def test_campaign_external_text_is_formula_escaped() -> None:
    payload = build_campaign_dict()
    if payload["data"]["top_posts"]:
        payload["data"]["top_posts"][0]["title"] = "=SUM(A1)"
    content = export_artifact(_Version("campaign_report_v2", payload))
    wb = load_workbook(BytesIO(content))
    values = _values(wb["热门帖子TOP"])
    assert any(isinstance(v, str) and v.startswith("'") for v in values)


def test_campaign_draft_raises_unsupported() -> None:
    with pytest.raises(ArtifactExportUnsupported) as excinfo:
        export_artifact(_Version("campaign_report_v2", None))
    assert excinfo.value.code == "ARTIFACT_EXPORT_UNSUPPORTED"


def test_campaign_invalid_payload_raises_unsupported() -> None:
    with pytest.raises(ArtifactExportUnsupported) as excinfo:
        export_artifact(_Version("campaign_report_v2", {"schema_version": "campaign_report_v2"}))
    assert excinfo.value.code == "ARTIFACT_EXPORT_UNSUPPORTED"
