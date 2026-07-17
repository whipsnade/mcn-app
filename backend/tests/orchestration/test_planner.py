from decimal import Decimal

import pytest

from app.mcp_gateway.contracts import DataTapService
from app.model.contracts import StructuredResult
from app.orchestration.planner import PlanValidationError, Planner
from app.orchestration.schemas import (
    PlannerContext,
    PlannerMessage,
    PlannerTool,
    SessionBrief,
    ToolPlan,
    ToolPlanStep,
)
from app.orchestration.analytics_contract import build_analytics_field_contract
from app.orchestration.export_contract import build_export_field_contract


_OBJECT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "name": {"type": "string"},
        "request": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "page": {"type": "integer"},
                "size": {"type": "integer"},
                "textContentWord": {"type": "string"},
            },
        },
    },
}


class StubModel:
    def __init__(self, value: ToolPlan) -> None:
        self.value = value

    async def complete_json(self, _request):
        return StructuredResult(value=self.value, usage=None, request_id=None, regeneration_count=0)


def _context(scope: str) -> PlannerContext:
    brief = SessionBrief(
        session_id="session-1",
        brand="科颜氏",
        campaign_name=None,
        platforms=("xiaohongshu", "douyin"),
        category="美妆",
        target_audience="",
        budget_min=Decimal("0"),
        budget_max=None,
        filters={},
    )
    tools = (
        PlannerTool(
            catalog_id="brand",
            internal_name="datatap.insight.social.statistic.trend.v1",
            service=DataTapService.INSIGHT_CUBE,
            description="品牌声量趋势",
            input_schema=_OBJECT_SCHEMA,
        ),
        PlannerTool(
            catalog_id="xhs",
            internal_name="datatap.xiaohongshu.kol.search.v1",
            service=DataTapService.SOCIAL_GROW,
            description="小红书达人检索",
            input_schema=_OBJECT_SCHEMA,
        ),
        PlannerTool(
            catalog_id="douyin",
            internal_name="datatap.douyin.kol.search.v1",
            service=DataTapService.SOCIAL_GROW,
            description="抖音达人检索",
            input_schema=_OBJECT_SCHEMA,
        ),
    )
    return PlannerContext(
        brief=brief,
        recent_messages=(PlannerMessage(role="user", content="分析科颜氏声量", sequence=1),),
        existing_results={},
        tools=tools,
        allowed_channels=("xiaohongshu", "douyin"),
        export_contract=build_export_field_contract(brief),
        analytics_contract=build_analytics_field_contract(),
        analysis_scope=scope,
        analysis_objectives=("brand_analysis",),
        requested_period={"unit": "month", "value": 3, "start": "2026-04-17", "end": "2026-07-17"},
    )


def _step(tool: str, kind: str) -> ToolPlanStep:
    return ToolPlanStep(
        id="step_1",
        internal_tool_name=tool,
        arguments={"name": "科颜氏"},
        evidence_kind=kind,
        evidence_goal="真实数据",
    )


@pytest.mark.asyncio
async def test_brand_plan_keeps_brand_tools_and_does_not_inject_kol_search() -> None:
    plan = ToolPlan(
        objective="品牌声量",
        steps=(_step("datatap.insight.social.statistic.trend.v1", "brand"),),
    )

    result = await Planner(model=StubModel(plan)).plan(_context("brand"))

    assert [step.evidence_kind for step in result.steps] == ["brand"]
    assert all("kol.search" not in step.internal_tool_name for step in result.steps)


@pytest.mark.asyncio
async def test_hybrid_plan_requires_both_evidence_kinds() -> None:
    plan = ToolPlan(
        objective="联合分析",
        steps=(_step("datatap.xiaohongshu.kol.search.v1", "kol"),),
    )

    with pytest.raises(PlanValidationError, match="EVIDENCE_SCOPE_NOT_COVERED"):
        await Planner(model=StubModel(plan)).plan(_context("hybrid"))
