"""brand_report_v3 Excel 导出（Task 18 / 设计 §12.1 消费边界）。

导出器只读已发布不可变 Version 的 payload，不调用模型/MCP；brand_report_v3
之外的类型（含 draft）抛 ``ArtifactExportUnsupported``（Task 19 路由映射 409
``ARTIFACT_EXPORT_UNSUPPORTED``）。
"""

from __future__ import annotations

from datetime import date
from io import BytesIO
from unittest.mock import Mock

import pytest
from openpyxl import load_workbook

from app.agent_artifacts.exporters import ArtifactExportUnsupported, export_artifact

from tests.agent_artifacts.test_payloads import build_brand_dict

BRAND_SHEETS = (
    "综合概览",
    "情感分析",
    "日趋势",
    "内容与达人",
    "地域与话题",
    "热门帖子TOP",
    "洞察与建议",
    "方法论",
)


class _Version:
    """轻量 Version 桩：模拟已发布 AgentArtifactVersion 的读取面。"""

    def __init__(self, schema_version: str, payload_json: dict | None) -> None:
        self.schema_version = schema_version
        self.payload_json = payload_json
        self.data_status = "complete" if payload_json else "draft"


def _brand_version(**overrides) -> _Version:
    payload = build_brand_dict()
    payload.update(overrides)
    return _Version("brand_report_v3", payload)


def _values(ws) -> list:
    return [
        cell.value
        for row in ws.iter_rows()
        for cell in row
        if cell.value is not None
    ]


def _brand_version_multi() -> _Version:
    """多点位 fixture：3 个日趋势点 + 2 个地域，覆盖图表区间计算（防单点掩盖 off-by-one）。"""
    payload = build_brand_dict()
    payload["data"]["daily_trend"] = [
        {
            "date": date(2026, 1, day),
            "platform": "xiaohongshu",
            "volume": day * 100,
            "engagement": day * 500,
            "positive": day * 70,
            "neutral": 20,
            "negative": 10,
        }
        for day in (1, 2, 3)
    ]
    payload["data"]["regions"] = [
        {"region": "上海", "volume": 300, "share": 0.3, "sentiment_score": 0.7},
        {"region": "北京", "volume": 700, "share": 0.7, "sentiment_score": 0.6},
    ]
    return _Version("brand_report_v3", payload)


def test_published_brand_report_exports_valid_workbook() -> None:
    """已发布 brand_report_v3 导出为可重载 .xlsx，八章节/关键单元格齐全。"""
    content = export_artifact(_brand_version_multi())
    assert content[:2] == b"PK"
    wb = load_workbook(BytesIO(content))
    assert tuple(wb.sheetnames) == BRAND_SHEETS

    # 概览：标题 + 核心指标 + 平台表现。
    overview = wb["综合概览"]
    assert "某品牌" in str(overview["A1"].value)
    assert 1000 in _values(overview)  # total_volume
    assert 5000 in _values(overview)  # total_engagement

    # 情感：正面/中性/负面计数与占比。
    sentiment = wb["情感分析"]
    assert 10 in _values(sentiment)  # 每个情感桶计数
    assert 0.5 in _values(sentiment)  # 占比

    # 日趋势：数据行 + 折线图引用全部数据点（3 行），不含表头行。
    trend = wb["日趋势"]
    assert any(
        isinstance(value, date) and value.year == 2026 and value.month == 1 and value.day == 1
        for value in _values(trend)
    )
    assert 100 in _values(trend)  # volume
    charts = trend._charts
    assert len(charts) == 1
    # openpyxl 排除表头行：数据引用从首个数据行（行 4）到末个数据行（行 6）。
    assert charts[0].ser[0].val.numRef.f == "'日趋势'!$C$4:$C$6"  # 声量列
    assert charts[0].ser[1].val.numRef.f == "'日趋势'!$D$4:$D$6"  # 互动列
    assert charts[0].ser[0].cat.numRef.f == "'日趋势'!$A$4:$A$6"  # 日期列

    # 话题：话题名 + 声量。
    topics = wb["地域与话题"]
    assert "咖啡" in _values(topics)
    assert 500 in _values(topics)

    # 热帖：标题/作者/互动渲染。
    top_posts = wb["热门帖子TOP"]
    assert "热帖" in _values(top_posts)
    assert "author" in _values(top_posts)
    assert 115 in _values(top_posts)


def test_region_chart_covers_region_values() -> None:
    """地域柱状图的数据区间必须落在真实数据行，不得把表头/合并标题混进数据或丢末行。"""
    payload = build_brand_dict()
    payload["data"]["regions"] = [
        {"region": "上海", "volume": 300, "share": 0.3, "sentiment_score": 0.7},
        {"region": "北京", "volume": 700, "share": 0.7, "sentiment_score": 0.6},
    ]
    wb = load_workbook(BytesIO(export_artifact(_Version("brand_report_v3", payload))))
    sheet = wb["地域与话题"]
    assert len(sheet._charts) == 1
    chart = sheet._charts[0]
    # write_table 布局：标题@3/表头@4/数据@5+；数据引用 B5:B6 覆盖两个地域真实值。
    assert chart.ser[0].val.numRef.f == "'地域与话题'!$B$5:$B$6"
    assert chart.ser[0].cat.numRef.f == "'地域与话题'!$A$5:$A$6"
    assert sheet["B4"].value == "声量"  # 表头仅作系列名，不进数据区间
    assert sheet["B5"].value == 300
    assert sheet["B6"].value == 700


def test_daily_trend_platform_cell_is_escaped() -> None:
    """平台名是第三方可控文本：以 = 开头时必须转义存为文本，不能成为公式。"""
    payload = build_brand_dict()
    payload["data"]["daily_trend"] = [
        {
            "date": date(2026, 1, 1),
            "platform": "=1+1",
            "volume": 100,
            "engagement": 500,
            "positive": 70,
            "neutral": 20,
            "negative": 10,
        }
    ]
    wb = load_workbook(BytesIO(export_artifact(_Version("brand_report_v3", payload))))
    cell = wb["日趋势"]["B4"]
    assert cell.value == "'=1+1"  # 前缀 ' 转义
    assert isinstance(cell.value, str) and cell.value.startswith("'")


def test_draft_brand_report_raises_unsupported() -> None:
    """未发布 draft（无可冻结 payload）→ 409 ARTIFACT_EXPORT_UNSUPPORTED。"""
    with pytest.raises(ArtifactExportUnsupported) as excinfo:
        export_artifact(_Version("brand_report_v3", None))
    assert excinfo.value.code == "ARTIFACT_EXPORT_UNSUPPORTED"


def test_invalid_published_payload_raises_unsupported() -> None:
    """历史/旁路非法 payload（强类型 ValidationError）→ 稳定 409，不泄漏 500。"""
    payload = build_brand_dict()
    payload["data"]["overview"] = {"totally": "wrong"}  # 结构非法
    with pytest.raises(ArtifactExportUnsupported) as excinfo:
        export_artifact(_Version("brand_report_v3", payload))
    assert excinfo.value.code == "ARTIFACT_EXPORT_UNSUPPORTED"
    assert excinfo.value.schema_version == "brand_report_v3"


def test_restricted_brand_discloses_restricted_sections() -> None:
    """data_status=restricted：受限章节保留列头 + 受限说明，不画误导性图表。"""
    payload = build_brand_dict()
    payload["data_status"] = "restricted"
    payload["limitations"] = [
        {
            "code": "source_down",
            "message": "数据源暂不可用",
            "affected_paths": ["data.daily_trend"],
        }
    ]
    payload["availability"]["daily_trend"] = {
        "status": "unavailable",
        "reason_codes": ["source_down"],
    }
    payload["data"]["daily_trend"] = []
    content = export_artifact(_Version("brand_report_v3", payload))
    wb = load_workbook(BytesIO(content))

    trend = wb["日趋势"]
    assert "数据受限" in str(_values(trend))
    assert "数据源暂不可用" in str(_values(trend))
    assert trend._charts == []  # 空章节不建图
    # 不受限章节仍正常渲染。
    assert 1000 in _values(wb["综合概览"])


def test_export_never_calls_model_or_mcp() -> None:
    """导出是表现层能力：传入的 model/gateway 桩绝不被调用。"""
    model = Mock()
    gateway = Mock()
    content = export_artifact(_brand_version(), model=model, gateway=gateway)
    assert content[:2] == b"PK"
    model.assert_not_called()
    gateway.assert_not_called()


__all__ = [
    "BRAND_SHEETS",
    "_Version",
    "_brand_version",
]
