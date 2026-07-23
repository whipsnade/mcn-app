import pytest

from app.goals.schemas import GoalParams, GoalPlannerOutput, GoalSpec
from app.goals.validation import GoalPlanSemanticError, validate_goal_plan


def _execute(*goals: GoalSpec) -> GoalPlannerOutput:
    return GoalPlannerOutput(action="execute", goals=list(goals))


def test_execute_rejects_duplicate_types_and_forward_dependency() -> None:
    duplicate = _execute(
        GoalSpec(
            sequence=1,
            goal_type="brand_analysis",
            params=GoalParams(brand="喜茶"),
            request_evidence="分析喜茶",
        ),
        GoalSpec(
            sequence=2,
            goal_type="brand_analysis",
            params=GoalParams(brand="奈雪"),
            request_evidence="对比奈雪",
        ),
    )
    with pytest.raises(GoalPlanSemanticError, match="duplicate_goal_type"):
        validate_goal_plan(duplicate, "分析喜茶并对比奈雪")

    forward = _execute(
        GoalSpec(
            sequence=1,
            goal_type="brand_analysis",
            depends_on_sequence=2,
            params=GoalParams(brand="喜茶"),
            request_evidence="分析喜茶",
        ),
        GoalSpec(
            sequence=2,
            goal_type="kol_selection",
            params=GoalParams(brand="喜茶"),
            request_evidence="圈选达人",
        ),
    )
    with pytest.raises(GoalPlanSemanticError, match="dependency_must_precede_goal"):
        validate_goal_plan(forward, "分析喜茶并圈选达人")


def test_campaign_requires_brand_and_campaign() -> None:
    output = _execute(
        GoalSpec(
            sequence=1,
            goal_type="campaign_analysis",
            params=GoalParams(brand="喜茶"),
            request_evidence="618 表现",
        )
    )
    with pytest.raises(GoalPlanSemanticError, match="campaign_scope_required"):
        validate_goal_plan(output, "分析喜茶 618 表现")


def test_brand_analysis_requires_brand() -> None:
    output = _execute(
        GoalSpec(
            sequence=1,
            goal_type="brand_analysis",
            params=GoalParams(),
            request_evidence="分析品牌表现",
        )
    )
    with pytest.raises(GoalPlanSemanticError, match="brand_scope_required"):
        validate_goal_plan(output, "分析品牌表现")


def test_kol_selection_requires_exact_current_message_evidence() -> None:
    output = _execute(
        GoalSpec(
            sequence=1,
            goal_type="kol_selection",
            params=GoalParams(brand="喜茶"),
            request_evidence="帮我圈选达人",
        )
    )
    with pytest.raises(GoalPlanSemanticError, match="selection_evidence_not_in_message"):
        validate_goal_plan(output, "分析喜茶最近一个月表现")


def test_campaign_then_selection_is_valid() -> None:
    output = _execute(
        GoalSpec(
            sequence=1,
            goal_type="campaign_analysis",
            params=GoalParams(brand="喜茶", campaign="618"),
            request_evidence="分析喜茶 618 表现",
        ),
        GoalSpec(
            sequence=2,
            goal_type="kol_selection",
            depends_on_sequence=1,
            params=GoalParams(brand="喜茶", campaign="618"),
            request_evidence="圈选下一轮达人",
        ),
    )
    validate_goal_plan(output, "分析喜茶 618 表现，并根据效果圈选下一轮达人")
