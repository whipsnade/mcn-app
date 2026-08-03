"""kol_selection_v3 Excel 导出（Task 18 / 设计 §12.1 消费边界）。

按 §12.1：Excel 每行展示八个 ``score_snapshot.dimensions.*.raw_score`` 列以及
总分/评级/星级/数据完整度，不展示 ``weighted_score`` 列；导出器不调用模型/MCP。
"""

from __future__ import annotations

from io import BytesIO
from unittest.mock import Mock

import pytest
from openpyxl import load_workbook

from app.agent_artifacts.exporters import ArtifactExportUnsupported, export_artifact

from tests.agent_artifacts.test_payloads import (
    EIGHT_DIMENSIONS,
    WEIGHTS,
    build_campaign_dict,
    build_insight_dict,
    build_kol_analysis_dict,
    build_kol_detail_dict,
    build_kol_selection_dict,
)

SUMMARY_HEADERS = (
    "序号",
    "平台",
    "昵称",
    "粉丝数",
    "互动总数",
    "平均互动",
    "行业兴趣",
    "目标地区",
    "目标年龄",
    "互动表现",
    "活跃粉丝",
    "内容质量",
    "粉丝规模",
    "互动粉丝比",
    "综合总分",
    "评级",
    "星级",
    "数据完整度",
)


class _Version:
    """轻量 Version 桩：模拟已发布 AgentArtifactVersion 的读取面。"""

    def __init__(self, schema_version: str, payload_json: dict | None) -> None:
        self.schema_version = schema_version
        self.payload_json = payload_json
        self.data_status = "complete" if payload_json else "draft"


def _item(rank: int, kol_uid: str, nickname: str, *, raw: float, total: float) -> dict:
    dimensions = {
        dim: {
            "raw_score": raw,
            "weight": WEIGHTS[dim],
            "weighted_score": round(raw * WEIGHTS[dim] / 100, 2),
            "source": "evidence:score_inputs",
            "missing_reason": None,
        }
        for dim in EIGHT_DIMENSIONS
    }
    return {
        "rank": rank,
        "platform": "xiaohongshu",
        "kol_uid": kol_uid,
        "nickname": nickname,
        "avatar_url": None,
        "homepage_url": f"https://x.com/{kol_uid}",
        "followers": 100000,
        "active_followers": 50000,
        "active_follower_rate": 50.0,
        "growth_rate": 2.0,
        "engagement_total": 5000,
        "avg_engagement": 50.0,
        "likes": 3000,
        "comments": 1000,
        "shares": 1000,
        "quoted_price": 1000,
        "reasons": ["互动高"],
        "missing_fields": [],
        "audience": {"regions": ["上海"], "age_ranges": ["25-34"], "interests": ["咖啡"]},
        "score_snapshot": {
            "version": "kol_score_v2",
            "total": total,
            "rating": "重点推荐" if total >= 78 else "推荐",
            "stars": "★★★★★" if total >= 78 else "★★★★",
            "data_completeness": 100.0,
            "dimensions": dimensions,
        },
    }


def _selection_version(items: list[dict] | None = None) -> _Version:
    payload = build_kol_selection_dict()
    if items is not None:
        payload["data"]["items"] = items
        payload["data"]["summary"]["selected_count"] = len(items)
    return _Version("kol_selection_v3", payload)


def test_published_kol_selection_exports_raw_score_columns() -> None:
    """每 KOL 行渲染八个 raw_score 列 + 总分/评级/星级/完整度，不用 weighted_score 顶替。"""
    items = [
        _item(1, "k1", "达人一", raw=80.0, total=78.0),
        _item(2, "k2", "达人二", raw=60.0, total=62.0),
    ]
    content = export_artifact(_selection_version(items))
    assert content[:2] == b"PK"
    wb = load_workbook(BytesIO(content))
    assert "KOL匹配度筛选" in wb.sheetnames

    summary = wb["KOL匹配度筛选"]
    headers = [summary.cell(4, column).value for column in range(1, 19)]
    assert tuple(headers) == SUMMARY_HEADERS
    # §12.1：不展示 weighted_score 列。
    assert not any("加权" in str(header) or "weighted" in str(header).lower() for header in headers)

    # 行 1：八维 raw_score 全为 80.0，总分/评级/星级/完整度齐全。
    row1 = [summary.cell(5, column).value for column in range(1, 19)]
    assert row1[6:14] == [80.0] * 8  # 八维 raw_score 列（industry_interest 若被 weighted 顶替会变 8.0）
    assert row1[14] == 78.0  # 综合总分
    assert row1[15] == "重点推荐"
    assert row1[16] == "★★★★★"
    assert row1[17] == 100.0  # 数据完整度

    # 行 2：raw_score 与行 1 不同，逐行独立渲染。
    row2 = [summary.cell(6, column).value for column in range(1, 19)]
    assert row2[6:14] == [60.0] * 8
    assert row2[14] == 62.0
    assert row2[17] == 100.0


@pytest.mark.parametrize(
    "schema_version,builder",
    [
        ("campaign_report_v2", build_campaign_dict),
        ("kol_analysis_v2", build_kol_analysis_dict),
        ("kol_detail_v2", build_kol_detail_dict),
        ("insight_board_v1", build_insight_dict),
    ],
)
def test_unsupported_artifact_types_raise_409(schema_version: str, builder) -> None:
    """不支持导出的 Artifact 类型 → 409 ARTIFACT_EXPORT_UNSUPPORTED。"""
    version = _Version(schema_version, builder())
    with pytest.raises(ArtifactExportUnsupported) as excinfo:
        export_artifact(version)
    assert excinfo.value.code == "ARTIFACT_EXPORT_UNSUPPORTED"
    assert excinfo.value.schema_version == schema_version


def test_draft_kol_selection_raises_unsupported() -> None:
    """未发布 draft（无可冻结 payload）→ 409 ARTIFACT_EXPORT_UNSUPPORTED。"""
    with pytest.raises(ArtifactExportUnsupported) as excinfo:
        export_artifact(_Version("kol_selection_v3", None))
    assert excinfo.value.code == "ARTIFACT_EXPORT_UNSUPPORTED"


def test_invalid_published_payload_raises_unsupported() -> None:
    """历史/旁路非法 payload（强类型 ValidationError）→ 稳定 409，不泄漏 500。"""
    payload = build_kol_selection_dict()
    payload["data"]["scoring"] = {"version": "kol_score_v1"}  # 非法评分块
    with pytest.raises(ArtifactExportUnsupported) as excinfo:
        export_artifact(_Version("kol_selection_v3", payload))
    assert excinfo.value.code == "ARTIFACT_EXPORT_UNSUPPORTED"
    assert excinfo.value.schema_version == "kol_selection_v3"


def test_export_never_calls_model_or_mcp() -> None:
    """导出是表现层能力：传入的 model/gateway 桩绝不被调用。"""
    model = Mock()
    gateway = Mock()
    content = export_artifact(_selection_version(), model=model, gateway=gateway)
    assert content[:2] == b"PK"
    model.assert_not_called()
    gateway.assert_not_called()
