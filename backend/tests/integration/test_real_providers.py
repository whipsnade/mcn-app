import pytest

from app.core.config import get_settings
from app.mcp_gateway.contracts import DataTapService
from app.mcp_gateway.datatap import DataTapTransport
from app.model.tencent_plan import TencentPlanAdapter


@pytest.mark.asyncio
async def test_real_datatap_lists_social_grow_tools() -> None:
    transport = DataTapTransport(token=get_settings().datatap_mcp_token)
    try:
        tools = await transport.list_tools(DataTapService.SOCIAL_GROW)
    finally:
        await transport.aclose()

    assert any(tool.name == "kol_xiaohongshu_search" for tool in tools)


def test_real_tencent_adapter_uses_confirmed_model() -> None:
    adapter = TencentPlanAdapter.from_settings(get_settings())
    assert adapter.model == "deepseek-v4-pro"
