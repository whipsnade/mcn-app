from decimal import Decimal

import pytest

from app.mcp_gateway.contracts import DataTapService
from app.model.contracts import StructuredResult
from app.model.contracts import ModelAdapterError
from app.orchestration.planner import Planner
from app.orchestration.schemas import (
    PlannerContext,
    PlannerMessage,
    PlannerTool,
    ReplanContext,
    ReplanFailure,
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
        self.requests = []

    async def complete_json(self, _request):
        self.requests.append(_request)
        return StructuredResult(value=self.value, usage=None, request_id=None, regeneration_count=0)


class FailingModel:
    async def complete_json(self, _request):
        raise ModelAdapterError("MODEL_UPSTREAM_ERROR", retryable=False)


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
async def test_brand_plan_adds_kol_evidence_and_becomes_hybrid_scope() -> None:
    plan = ToolPlan(
        objective="品牌声量",
        steps=(_step("datatap.insight.social.statistic.trend.v1", "brand"),),
    )

    model = StubModel(plan)
    result = await Planner(model=model).plan(_context("brand"))

    assert any(step.evidence_kind == "brand" for step in result.steps)
    assert any(step.evidence_kind == "kol" for step in result.steps)
    assert result.analysis_scope == "hybrid"
    assert model.requests[0].max_tokens == 8192


@pytest.mark.asyncio
async def test_brand_and_kol_steps_are_persisted_as_hybrid_scope() -> None:
    plan = ToolPlan(
        objective="品牌声量与匹配达人",
        primary_intent="brand",
        objectives=("volume_trend",),
        steps=(
            _step("datatap.insight.social.statistic.trend.v1", "brand"),
            ToolPlanStep(
                id="step_3",
                internal_tool_name="datatap.xiaohongshu.kol.search.v1",
                arguments={"name": "科颜氏"},
                evidence_kind="kol",
                evidence_goal="匹配相关达人",
            ),
        ),
    )

    result = await Planner(model=StubModel(plan)).plan(_context("brand"))

    assert {step.evidence_kind for step in result.steps} == {"brand", "kol"}
    assert result.analysis_scope == "hybrid"


@pytest.mark.asyncio
async def test_hybrid_plan_requires_both_evidence_kinds() -> None:
    plan = ToolPlan(
        objective="联合分析",
        primary_intent="hybrid",
        steps=(_step("datatap.xiaohongshu.kol.search.v1", "kol"),),
    )

    result = await Planner(model=StubModel(plan)).plan(_context("hybrid"))

    assert result.analysis_scope == "hybrid"
    assert {step.evidence_kind for step in result.steps} == {"brand", "kol"}


@pytest.mark.asyncio
async def test_replan_can_add_brand_only_after_kol_evidence_succeeded() -> None:
    plan = ToolPlan(
        objective="补充品牌趋势",
        primary_intent="brand",
        steps=(
            ToolPlanStep(
                id="step_3",
                internal_tool_name="datatap.insight.social.statistic.trend.v1",
                arguments={"name": "科颜氏"},
                evidence_kind="brand",
                evidence_goal="补充品牌趋势",
            ),
        ),
    )
    recovery = ReplanContext(
        completed_step_ids=("step_1",),
        completed_evidence_kinds=("kol",),
        failed_steps=(
            ReplanFailure(
                step_id="step_2",
                internal_tool_name="datatap.insight.social.statistic.trend.v1",
                error_code="upstream_error",
            ),
        ),
        remaining_calls=1,
        remaining_points=10,
    )

    result = await Planner(model=StubModel(plan)).replan(_context("kol"), recovery)

    assert result.analysis_scope == "brand"
    assert result.steps[0].evidence_kind == "brand"


@pytest.mark.asyncio
async def test_model_failure_uses_router_only_as_safe_fallback() -> None:
    result = await Planner(model=FailingModel()).plan(_context("kol"))

    assert any(step.evidence_kind == "kol" for step in result.steps)
    assert result.analysis_scope in {"kol", "hybrid"}


@pytest.mark.asyncio
async def test_planner_drops_undeclared_optional_arguments() -> None:
    plan = ToolPlan(
        objective="品牌声量",
        steps=(
            ToolPlanStep(
                id="step_1",
                internal_tool_name="datatap.insight.social.statistic.trend.v1",
                arguments={"name": "科颜氏", "metrics": ["声量", "互动数"]},
                evidence_kind="brand",
                evidence_goal="真实品牌趋势",
            ),
        ),
    )

    result = await Planner(model=StubModel(plan)).plan(_context("brand"))

    assert result.steps[0].arguments == {"name": "科颜氏"}


@pytest.mark.asyncio
async def test_planner_expands_profile_media_to_selected_platforms() -> None:
    context = _context("hybrid").model_copy(
        update={
            "tools": _context("hybrid").tools
            + (
                PlannerTool(
                    catalog_id="profile",
                    internal_name="datatap.insight.social.statistic.user.profile.v1",
                    service=DataTapService.INSIGHT_CUBE,
                    description="用户画像",
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "media": {"type": "string", "enum": ["小红书", "抖音", "微博"]},
                        },
                        "required": ["media"],
                    },
                ),
            ),
        }
    )
    plan = ToolPlan(
        objective="对比粉丝画像",
        steps=(
            ToolPlanStep(
                id="step_1",
                internal_tool_name="datatap.insight.social.statistic.user.profile.v1",
                arguments={"media": "{{依次为小红书、抖音}}"},
                evidence_kind="brand",
                evidence_goal="对比用户画像",
            ),
            _step("datatap.xiaohongshu.kol.search.v1", "kol").model_copy(update={"id": "step_2"}),
        ),
    )

    result = await Planner(model=StubModel(plan)).plan(context)

    profile_steps = [
        step for step in result.steps
        if step.internal_tool_name == "datatap.insight.social.statistic.user.profile.v1"
    ]
    assert [step.arguments["media"] for step in profile_steps] == ["小红书", "抖音"]
    assert [step.id for step in result.steps] == ["step_1", "step_2", "step_3", "step_4"]


_DETAIL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "platform": {"type": "string"},
        "kwUidList": {"type": "array", "items": {"type": "string"}},
        "scope": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["platform", "kwUidList", "scope"],
}


def _detail_context() -> PlannerContext:
    context = _context("kol")
    detail_tool = PlannerTool(
        catalog_id="detail",
        internal_name="datatap.social.grow.kol.detail.v1",
        service=DataTapService.SOCIAL_GROW,
        description="达人详情",
        input_schema=_DETAIL_SCHEMA,
    )
    return context.model_copy(update={"tools": (*context.tools, detail_tool)})


def _detail_step(uids: list[str]) -> ToolPlanStep:
    return ToolPlanStep(
        id="step_2",
        internal_tool_name="datatap.social.grow.kol.detail.v1",
        arguments={
            "platform": "douyin",
            "kwUidList": uids,
            "scope": ["fansAudience"],
        },
        evidence_kind="kol",
        evidence_goal="达人详情",
    )


@pytest.mark.asyncio
async def test_empty_uid_detail_steps_are_kept_for_executor_backfill() -> None:
    """空 uid 详情步骤保留在计划中：执行器会用搜索结果回填或跳过。"""
    plan = ToolPlan(
        objective="达人分析",
        steps=(
            ToolPlanStep(
                id="step_1",
                internal_tool_name="datatap.xiaohongshu.kol.search.v1",
                arguments={"name": "星巴克"},
                evidence_kind="kol",
                evidence_goal="匹配达人",
            ),
            _detail_step([]),
        ),
    )

    result = await Planner(model=StubModel(plan)).plan(_detail_context())

    names = [step.internal_tool_name for step in result.steps]
    assert "datatap.social.grow.kol.detail.v1" in names
    assert "datatap.xiaohongshu.kol.search.v1" in names


@pytest.mark.asyncio
async def test_detail_steps_with_real_uids_are_kept() -> None:
    plan = ToolPlan(
        objective="达人分析",
        steps=(
            ToolPlanStep(
                id="step_1",
                internal_tool_name="datatap.xiaohongshu.kol.search.v1",
                arguments={"name": "星巴克"},
                evidence_kind="kol",
                evidence_goal="匹配达人",
            ),
            _detail_step(["uid-1"]),
        ),
    )

    result = await Planner(model=StubModel(plan)).plan(_detail_context())

    names = [step.internal_tool_name for step in result.steps]
    assert "datatap.social.grow.kol.detail.v1" in names


@pytest.mark.asyncio
async def test_plan_with_only_empty_detail_calls_stays_valid() -> None:
    plan = ToolPlan(objective="达人分析", steps=(_detail_step([]),))

    result = await Planner(model=StubModel(plan)).plan(_detail_context())

    names = [step.internal_tool_name for step in result.steps]
    assert "datatap.social.grow.kol.detail.v1" in names


@pytest.mark.asyncio
async def test_search_only_plan_gets_detail_step_appended() -> None:
    """只有搜索步骤的计划自动补一个空 uid 详情步骤（执行器回填）。"""
    plan = ToolPlan(
        objective="达人分析",
        steps=(
            ToolPlanStep(
                id="step_1",
                internal_tool_name="datatap.xiaohongshu.kol.search.v1",
                arguments={"name": "科颜氏"},
                evidence_kind="kol",
                evidence_goal="匹配达人",
            ),
        ),
    )

    result = await Planner(model=StubModel(plan)).plan(_detail_context())

    detail_steps = [
        step for step in result.steps
        if step.internal_tool_name == "datatap.social.grow.kol.detail.v1"
    ]
    assert len(detail_steps) == 1
    assert detail_steps[0].arguments["kwUidList"] == []
    assert detail_steps[0].arguments["scope"]
    assert detail_steps[0].depends_on


@pytest.mark.asyncio
async def test_plan_with_existing_detail_step_is_not_duplicated() -> None:
    plan = ToolPlan(
        objective="达人分析",
        steps=(
            ToolPlanStep(
                id="step_1",
                internal_tool_name="datatap.xiaohongshu.kol.search.v1",
                arguments={"name": "科颜氏"},
                evidence_kind="kol",
                evidence_goal="匹配达人",
            ),
            _detail_step(["uid-1"]),
        ),
    )

    result = await Planner(model=StubModel(plan)).plan(_detail_context())

    detail_steps = [
        step for step in result.steps
        if step.internal_tool_name == "datatap.social.grow.kol.detail.v1"
    ]
    assert len(detail_steps) == 1
