from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.mcp_gateway.contracts import DataTapService
from app.model.contracts import StructuredResult
from app.model.fake import FakeModelAdapter
from app.orchestration.planner import PlanValidationError, Planner
from app.orchestration.schemas import (
    PlannerContext,
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
