import pytest
from pydantic import BaseModel, ConfigDict

from app.core.config import get_settings
from app.mcp_gateway.contracts import DataTapService
from app.mcp_gateway.datatap import DataTapTransport
from app.model.contracts import ChatMessage, StructuredModelRequest
from app.model.tencent_plan import TencentPlanAdapter


class ProbeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool


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


@pytest.mark.asyncio
async def test_real_tencent_adapter_recovers_from_json_schema_incompatibility() -> None:
    adapter = TencentPlanAdapter.from_settings(get_settings())
    try:
        result = await adapter.complete_json(
            StructuredModelRequest(
                purpose="planner",
                template_name="provider_probe",
                messages=(
                    ChatMessage(role="system", content="只返回符合 JSON Schema 的结果。"),
                    ChatMessage(role="user", content='返回 {"ok": true}'),
                ),
                output_model=ProbeResult,
                max_tokens=256,
            )
        )
    finally:
        await adapter.aclose()

    assert result.value.ok is True
