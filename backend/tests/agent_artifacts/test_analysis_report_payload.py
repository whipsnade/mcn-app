"""analysis_report_v1 强类型 payload 与模型输入契约的 RED→GREEN 测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agent_artifacts.model_inputs.analysis_report import (
    AnalysisReportV1Input,
    assemble_analysis_report_payload,
)
from app.agent_artifacts.payloads.analysis_report import AnalysisReportV1


METHODOLOGY = {
    "data_as_of": "2026-01-15T12:00:00",
    "source_names": ["DataTap"],
    "notes": [],
}


def _base_input(*, blocks: list[dict] | None = None, fulfillment: list[dict] | None = None) -> dict:
    return {
        "title": "跨平台营销分析",
        "subject_type": "mixed",
        "scope": {
            "brand": "示例品牌",
            "platforms": ["xiaohongshu", "douyin"],
            "period": {"start": "2026-01-01", "end": "2026-01-31", "timezone": "Asia/Shanghai"},
        },
        "blocks": blocks or [{
            "block_type": "metric_cards",
            "id": "summary-metrics",
            "title": "摘要指标",
            "cards": [{"key": "reach", "label": "触达", "value": 1234, "unit": "人"}],
        }],
        "fulfillment": fulfillment or [{
            "key": "creators",
            "requested_min": 40,
            "actual_count": 40,
            "status": "complete",
            "reason": "已返回满足条件的达人数量",
        }],
        "availability": {
            "blocks": {"status": "complete", "reason_codes": []},
        },
        "limitations": [],
        "methodology_input": METHODOLOGY,
    }


def _all_blocks() -> list[dict]:
    return [
        {
            "block_type": "metric_cards",
            "id": "metrics",
            "title": "指标",
            "cards": [{"key": "ctr", "label": "点击率", "value": 0.12, "unit": "%"}],
        },
        {
            "block_type": "typed_table",
            "id": "table",
            "title": "跨平台统一表头",
            "columns": [
                {"key": "name", "label": "名称", "type": "string"},
                {"key": "count", "label": "数量", "type": "integer"},
                {"key": "score", "label": "分数", "type": "number"},
                {"key": "rate", "label": "比例", "type": "percent"},
                {"key": "day", "label": "日期", "type": "date"},
                {"key": "at", "label": "时间", "type": "datetime"},
                {"key": "url", "label": "链接", "type": "url"},
                {"key": "enabled", "label": "启用", "type": "boolean"},
            ],
            "rows": [[
                "达人 A", 10, 1.5, 0.2, "2026-01-01", "2026-01-01T12:00:00",
                "https://example.com/post/1", True,
            ]],
        },
        {
            "block_type": "time_series",
            "id": "trend",
            "title": "趋势",
            "points": [{"timestamp": "2026-01-01", "values": {"reach": 10, "engagement": 2.5}}],
        },
        {
            "block_type": "link_list",
            "id": "links",
            "title": "参考链接",
            "items": [{"label": "原帖", "url": "https://example.com/post/1", "description": "说明"}],
        },
        {
            "block_type": "chart",
            "id": "chart",
            "title": "平台对比",
            "chart_type": "bar",
            "categories": ["小红书", "抖音"],
            "series": [{"key": "reach", "label": "触达", "values": [10, 20]}],
        },
        {
            "block_type": "narrative",
            "id": "narrative",
            "title": "结论",
            "content": "不同平台的表现存在差异。",
            "supporting_paths": [],
        },
        {
            "block_type": "methodology_limitations",
            "id": "method",
            "title": "方法与限制",
            "methodology": "按平台汇总真实返回结果。",
            "limitations": ["部分平台数据可能延迟"],
        },
    ]


def test_analysis_report_supports_all_typed_blocks_and_server_assembly() -> None:
    model_input = AnalysisReportV1Input.model_validate(_base_input(blocks=_all_blocks()))
    payload = assemble_analysis_report_payload(model_input)

    assert payload["schema_version"] == "analysis_report_v1"
    assert payload["module"] == "report"
    assert payload["data_status"] == "complete"
    assert {block["block_type"] for block in payload["blocks"]} == {
        "metric_cards",
        "typed_table",
        "time_series",
        "link_list",
        "chart",
        "narrative",
        "methodology_limitations",
    }
    report = AnalysisReportV1.model_validate(payload)
    assert report.blocks[1].columns[3].type == "percent"


def test_partial_fulfillment_preserves_actual_count_and_discloses_limitation() -> None:
    model_input = AnalysisReportV1Input.model_validate(_base_input(
        fulfillment=[{
            "key": "creators",
            "requested_min": 40,
            "actual_count": 37,
            "status": "partial",
            "reason": "真实数据仅返回 37 位达人",
        }]
    ))
    payload = assemble_analysis_report_payload(model_input)

    assert payload["data_status"] == "restricted"
    assert payload["fulfillment"][0]["actual_count"] == 37
    assert any(item["code"] == "fulfillment_partial" for item in payload["limitations"])
    assert AnalysisReportV1.model_validate(payload).data_status == "restricted"


def test_report_has_no_business_row_cap() -> None:
    rows = [[f"达人-{index}"] for index in range(201)]
    blocks = [{
        "block_type": "typed_table",
        "id": "all-creators",
        "title": "全部达人",
        "columns": [{"key": "name", "label": "名称", "type": "string"}],
        "rows": rows,
    }]
    model_input = AnalysisReportV1Input.model_validate(_base_input(
        blocks=blocks,
        fulfillment=[{
            "key": "creators",
            "requested_min": 200,
            "actual_count": 201,
            "status": "complete",
            "reason": "真实返回 201 条",
        }],
    ))
    report = AnalysisReportV1.model_validate(assemble_analysis_report_payload(model_input))
    assert len(report.blocks[0].rows) == 201


@pytest.mark.parametrize(
    "bad_block",
    [
        {
            "block_type": "link_list",
            "id": "bad-url",
            "title": "链接",
            "items": [{"label": "x", "url": "ftp://example.com"}],
        },
        {
            "block_type": "typed_table",
            "id": "formula",
            "title": "公式",
            "columns": [{"key": "value", "label": "值", "type": "string"}],
            "rows": [["=HYPERLINK(\"https://evil.invalid\")"]],
        },
        {
            "block_type": "narrative",
            "id": "secret",
            "title": "敏感信息",
            "content": "Bearer abcdefghijk",
        },
    ],
)
def test_report_rejects_unsafe_url_formula_or_secret(bad_block: dict) -> None:
    with pytest.raises((ValidationError, ValueError)):
        AnalysisReportV1Input.model_validate(_base_input(blocks=[bad_block]))


def test_report_rejects_duplicate_block_ids_and_server_owned_fields() -> None:
    duplicate = _base_input(blocks=[_all_blocks()[0], {**_all_blocks()[0], "title": "重复"}])
    with pytest.raises(ValidationError):
        AnalysisReportV1Input.model_validate(duplicate)

    polluted = _base_input()
    polluted["schema_version"] = "analysis_report_v1"
    with pytest.raises(ValidationError):
        AnalysisReportV1Input.model_validate(polluted)


def test_payload_data_status_is_not_model_controllable() -> None:
    payload = assemble_analysis_report_payload(
        AnalysisReportV1Input.model_validate(_base_input(
            fulfillment=[{
                "key": "creators",
                "requested_min": 40,
                "actual_count": 37,
                "status": "partial",
                "reason": "真实数据不足",
            }]
        ))
    )
    payload["data_status"] = "complete"
    with pytest.raises(ValidationError):
        AnalysisReportV1.model_validate(payload)
