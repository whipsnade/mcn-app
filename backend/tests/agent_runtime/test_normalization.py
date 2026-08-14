"""Normalization Registry 诊断测试（Gate B：DataTap 成功 payload 字段映射）。

验证：成功返回的「日/周」时间别名进入 period_key；未识别业务字段进入
unmapped_fields 诊断（incomplete 而非 no evidence）；无 adapter 工具
not_applicable 不误报失败。
"""

from __future__ import annotations

from app.agent_runtime.normalization import NormalizationRegistry


def test_trend_normalizer_accepts_chinese_day_and_week() -> None:
    registry = NormalizationRegistry()
    result = registry.normalize(
        "query_analysis_data",
        {"data": [{"日": "2026-08-01", "声量": 12}, {"周": "2026-W31", "声量": 20}]},
    )
    assert result.status == "normalized"
    assert result.preview is not None
    rows = result.preview["rows"]
    assert rows[0]["period_key"] == "2026-08-01"
    assert rows[0]["volume"] == 12
    assert rows[1]["period_key"] == "2026-W31"
    assert result.field_mapping == {"日": "period_key", "周": "period_key", "声量": "volume"}
    assert result.unmapped_fields == ()


def test_unknown_business_fields_are_diagnostic_not_no_evidence() -> None:
    registry = NormalizationRegistry()
    result = registry.normalize(
        "social_statistic_trend", {"data": [{"新时间粒度": "上午", "声量": 8}]}
    )
    assert result.status == "incomplete"
    assert "新时间粒度" in result.unmapped_fields
    # 已识别字段仍进入 field_mapping：诊断只是提示，不否定 Evidence 本身。
    assert result.field_mapping["声量"] == "volume"
    assert result.preview["rows"][0]["volume"] == 8


def test_tool_without_adapter_is_not_applicable_not_failed() -> None:
    registry = NormalizationRegistry()
    result = registry.normalize("some_unregistered_tool", {"data": [{"声量": 1}]})
    assert result.status == "not_applicable"
    assert result.error_code is None
    assert result.unmapped_fields == ()


def test_week_period_key_is_preserved_not_parsed_as_date() -> None:
    registry = NormalizationRegistry()
    result = registry.normalize(
        "query_analysis_data", {"data": [{"周": "2026-W31", "声量": 20}]}
    )
    assert result.status == "normalized"
    assert result.preview["rows"][0]["period_key"] == "2026-W31"
