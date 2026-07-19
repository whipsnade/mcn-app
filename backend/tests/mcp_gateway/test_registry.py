from app.mcp_gateway.contracts import DataTapService
from app.mcp_gateway.registry import DYNAMIC_TOOL_ALLOWLIST, close_input_schema


def test_real_brand_and_all_channel_kol_tools_are_explicitly_allowlisted() -> None:
    insight = DYNAMIC_TOOL_ALLOWLIST[DataTapService.INSIGHT_CUBE]
    social = DYNAMIC_TOOL_ALLOWLIST[DataTapService.SOCIAL_GROW]

    assert {"query_analysis_data", "social_statistic_trend"} <= set(insight)
    assert {
        "kol_xiaohongshu_search",
        "kol_douyin_search",
        "kol_bilibili_search",
        "kol_weibo_search",
        "kol_wechat_search",
    } <= set(social)


def test_unrelated_remote_tools_are_not_allowlisted() -> None:
    assert "stock_prices" not in DYNAMIC_TOOL_ALLOWLIST.get(DataTapService.AKTOOLS, {})


def test_close_input_schema_closes_nested_provider_objects() -> None:
    schema = {
        "type": "object",
        "properties": {
            "filters": {
                "type": "object",
                "properties": {
                    "regions": {
                        "type": "array",
                        "items": {"type": "object", "properties": {"name": {"type": "string"}}},
                    }
                },
            }
        },
        "additionalProperties": {"type": "string"},
    }

    normalized = close_input_schema(schema)

    assert normalized["additionalProperties"] is False
    assert normalized["properties"]["filters"]["additionalProperties"] is False
    assert (
        normalized["properties"]["filters"]["properties"]["regions"]["items"]["additionalProperties"]
        is False
    )
    # Normalization must not mutate the live discovery object.
    assert schema["additionalProperties"] is not False


def test_kol_detail_description_documents_valid_scope_values() -> None:
    """模型只能靠审核描述了解 scope 词表，缺失会导致详情调用空转。"""
    description = DYNAMIC_TOOL_ALLOWLIST[DataTapService.SOCIAL_GROW]["kol_detail"][1]

    assert "fansAudience" in description
    assert "accountTrend" in description
    assert "businessBrand" in description
