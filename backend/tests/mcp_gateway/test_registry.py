from app.mcp_gateway.contracts import DataTapService
from app.mcp_gateway.registry import DYNAMIC_TOOL_ALLOWLIST


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
