import os

import pytest
from pydantic import BaseModel, ConfigDict

from app.core.config import get_settings
from app.mcp_gateway.contracts import DataTapService
from app.mcp_gateway.datatap import DataTapTransport
from app.model.contracts import ChatMessage, StructuredModelRequest
from app.model.tencent_plan import TencentPlanAdapter

# 真实服务探针：命中真实模型/DataTap，默认跳过（结果受供应商行为影响，不宜在
# 默认套件中造成非确定性红）；仅 RUN_REAL_SERVICES=1 + -m real_services 时运行。
_REAL_SERVICES_SKIP_REASON = "真实服务探针需要 RUN_REAL_SERVICES=1（scripts/run_real_agent_uat.sh）"


class ProbeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool


@pytest.mark.asyncio
@pytest.mark.real_services
@pytest.mark.skipif(
    os.environ.get("RUN_REAL_SERVICES") != "1", reason=_REAL_SERVICES_SKIP_REASON
)
async def test_real_datatap_lists_social_grow_tools() -> None:
    transport = DataTapTransport(token=get_settings().datatap_mcp_token)
    try:
        tools = await transport.list_tools(DataTapService.SOCIAL_GROW)
    finally:
        await transport.aclose()

    assert any(tool.name == "kol_xiaohongshu_search" for tool in tools)


def test_real_tencent_adapter_uses_confirmed_model() -> None:
    settings = get_settings()
    adapter = TencentPlanAdapter.from_settings(settings)
    # 不断言具体模型名：TENCENT_PLAN_MODEL 随部署配置（.env）漂移，只校验适配器
    # 使用当前 settings 生效的模型。
    assert adapter.model == settings.tencent_plan_model


@pytest.mark.asyncio
@pytest.mark.real_services
@pytest.mark.skipif(
    os.environ.get("RUN_REAL_SERVICES") != "1", reason=_REAL_SERVICES_SKIP_REASON
)
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
