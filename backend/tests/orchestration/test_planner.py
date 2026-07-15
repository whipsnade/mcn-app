from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.mcp_gateway.contracts import DataTapService
from app.model.contracts import StructuredResult
from app.model.fake import FakeModelAdapter
from app.orchestration.planner import PlanValidationError, Planner
from app.orchestration.schemas import (
    PlannerContext,
    PlannerMessage,
    PlannerTool,
    SessionBrief,
    ToolPlan,
    ToolPlanStep,
)


def _tool() -> PlannerTool:
    return PlannerTool(
        catalog_id="catalog-1",
        internal_name="kol.search",
        service=DataTapService.BILIBILI,
        input_schema={
            "type": "object",
            "properties": {"keyword": {"type": "string", "minLength": 1}},
            "required": ["keyword"],
            "additionalProperties": False,
        },
    )


def _xiaohongshu_tool() -> PlannerTool:
    return PlannerTool(
        catalog_id="catalog-xhs-1",
        internal_name="datatap.xiaohongshu.kol.search.v1",
        service=DataTapService.SOCIAL_GROW,
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "request": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "page": {"type": "integer"},
                        "size": {"type": "integer"},
                        "followercountMin": {"type": "integer"},
                        "kwProvinceList": {"type": "array", "items": {"type": "string"}},
                        "sexListFan": {"type": "array", "items": {"type": "string"}},
                        "sexListFanMin": {"type": "number"},
                        "ageListFan": {"type": "array", "items": {"type": "string"}},
                        "ageListFanMin": {"type": "number"},
                        "sumpostMin": {"type": "integer"},
                        "textContentWord": {"type": "string"},
                    },
                }
            },
            "required": ["request"],
        },
    )


def context_fixture(
    *,
    allowed_channels: tuple[str, ...] = ("bilibili",),
    platforms: tuple[str, ...] = ("bilibili",),
) -> PlannerContext:
    return PlannerContext(
        brief=SessionBrief(
            session_id="session-1",
            brand="测试品牌",
            campaign_name="新品推广",
            platforms=platforms,
            category="美妆",
            target_audience="学生",
            budget_min=None,
            budget_max=None,
            filters={},
        ),
        recent_messages=(),
        existing_results={},
        tools=(_tool(),),
        allowed_channels=allowed_channels,
    )


def plan_with_tool(name: str) -> ToolPlan:
    return ToolPlan(
        objective="寻找合适达人",
        steps=(
            ToolPlanStep(
                id="step_1",
                internal_tool_name=name,
                arguments={"keyword": "美妆"},
                evidence_goal="候选达人列表",
            ),
        ),
    )


def plan_with_repeated_calls(count: int) -> ToolPlan:
    return ToolPlan.model_construct(
        objective="寻找合适达人",
        steps=tuple(
            ToolPlanStep(
                id=f"step_{index}",
                internal_tool_name="kol.search",
                arguments={"keyword": "美妆"},
                evidence_goal="候选达人列表",
            )
            for index in range(1, count + 1)
        ),
    )


def _planner(result: ToolPlan) -> tuple[Planner, FakeModelAdapter]:
    model = FakeModelAdapter(
        structured_results=(
            StructuredResult(value=result, usage=None, request_id="model-1", regeneration_count=0),
        )
    )
    return Planner(model=model), model


@pytest.mark.asyncio
async def test_plan_rejects_disabled_service_before_mcp() -> None:
    planner, fake_model = _planner(plan_with_tool("google-trends-mcp.search"))
    fake_mcp_transport = SimpleNamespace(call_count=0)

    with pytest.raises(PlanValidationError) as caught:
        await planner.plan(context_fixture())

    assert caught.value.code == "TOOL_NOT_ALLOWED"
    assert fake_mcp_transport.call_count == 0
    assert len(fake_model.structured_requests) == 1


@pytest.mark.asyncio
async def test_plan_rejects_more_than_ten_calls() -> None:
    planner, _ = _planner(plan_with_repeated_calls(count=11))

    with pytest.raises(PlanValidationError) as caught:
        await planner.plan(context_fixture())

    assert caught.value.code == "TOO_MANY_TOOL_CALLS"


@pytest.mark.asyncio
async def test_plan_rejects_arguments_outside_approved_schema() -> None:
    planner, _ = _planner(
        ToolPlan(
            objective="寻找合适达人",
            steps=(
                ToolPlanStep(
                    id="step_1",
                    internal_tool_name="kol.search",
                    arguments={"keyword": "美妆", "url": "https://unsafe.invalid"},
                    evidence_goal="候选达人列表",
                ),
            ),
        )
    )

    with pytest.raises(PlanValidationError) as caught:
        await planner.plan(context_fixture())

    assert caught.value.code == "INVALID_TOOL_ARGUMENTS"


@pytest.mark.asyncio
async def test_plan_rejects_session_channel_without_user_permission() -> None:
    planner, _ = _planner(plan_with_tool("kol.search"))

    with pytest.raises(PlanValidationError) as caught:
        await planner.plan(context_fixture(allowed_channels=()))

    assert caught.value.code == "CHANNEL_NOT_ALLOWED"


@pytest.mark.asyncio
async def test_plan_rejects_tool_service_outside_user_channels() -> None:
    planner, _ = _planner(plan_with_tool("kol.search"))

    with pytest.raises(PlanValidationError) as caught:
        await planner.plan(
            context_fixture(
                allowed_channels=("xiaohongshu",),
                platforms=("xiaohongshu",),
            )
        )

    assert caught.value.code == "SERVICE_CHANNEL_NOT_ALLOWED"


@pytest.mark.asyncio
async def test_plan_compiles_supported_defaults_for_datatap_xiaohongshu_search() -> None:
    context = context_fixture(allowed_channels=("xiaohongshu",), platforms=("xiaohongshu",))
    context = context.model_copy(
        update={
            "brief": context.brief.model_copy(
                update={
                    "brand": "科颜氏",
                    "target_audience": "20～30女性",
                    "filters": {"target_fan_locations": ["湖州", "浙江"]},
                }
            ),
            "recent_messages": (
                PlannerMessage(role="user", content="找最近30天活跃 top10 达人", sequence=1),
            ),
            "tools": (_xiaohongshu_tool(),),
        }
    )
    plan = ToolPlan(
        objective="找达人",
        steps=(
            ToolPlanStep(
                id="step_1",
                internal_tool_name="datatap.xiaohongshu.kol.search.v1",
                arguments={
                    "request": {
                        "followercountMin": 20_000,
                        "kwProvinceList": ["湖州", "浙江"],
                        "ageListFan": ["age1PercentFan", "age2PercentFan"],
                    }
                },
                evidence_goal="候选达人列表",
            ),
        ),
    )
    planner, _ = _planner(plan)

    result = await planner.plan(context)

    assert result.steps[0].arguments == {
        "request": {
            "page": 1,
            "size": 10,
            "followercountMin": 20_000,
            "kwProvinceList": ["浙江省"],
            "sexListFan": ["femalePercentFan"],
            "sexListFanMin": 0.5,
            "ageListFan": ["age2PercentFan", "age3PercentFan"],
            "ageListFanMin": 0.2,
            "sumpostMin": 1,
            "textContentWord": "科颜氏",
        }
    }
