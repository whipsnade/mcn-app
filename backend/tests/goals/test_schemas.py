import pytest
from pydantic import ValidationError

from app.goals.schemas import GoalParams, GoalPlannerOutput, GoalQuestion, GoalSpec


def test_clarify_requires_question_and_forbids_goals() -> None:
    with pytest.raises(ValidationError):
        GoalPlannerOutput(action="clarify", question=None, goals=[])
    with pytest.raises(ValidationError):
        GoalPlannerOutput(
            action="clarify",
            question=GoalQuestion(text="请确认品牌", options=[]),
            goals=[
                GoalSpec(
                    sequence=1,
                    goal_type="brand_analysis",
                    params=GoalParams(brand="喜茶"),
                    request_evidence="分析喜茶",
                )
            ],
        )


def test_goal_params_comparison_mode_defaults_to_mom() -> None:
    assert GoalParams().comparison_mode == "mom"


def test_goal_params_accepts_mom_yoy_and_rejects_invalid_values() -> None:
    assert GoalParams(comparison_mode="mom_yoy").comparison_mode == "mom_yoy"
    with pytest.raises(ValidationError):
        GoalParams(comparison_mode="yoy")
    with pytest.raises(ValidationError):
        GoalParams(comparison_mode="")


def test_goal_params_parses_legacy_payload_without_comparison_mode() -> None:
    params = GoalParams.model_validate({"brand": "喜茶", "requirement": "分析声量"})
    assert params.comparison_mode == "mom"


def test_execute_requires_one_to_three_goals_and_forbids_question() -> None:
    with pytest.raises(ValidationError):
        GoalPlannerOutput(action="execute", question=None, goals=[])
    with pytest.raises(ValidationError):
        GoalPlannerOutput(
            action="execute",
            question=GoalQuestion(text="多余问题", options=[]),
            goals=[
                GoalSpec(
                    sequence=1,
                    goal_type="brand_analysis",
                    params=GoalParams(brand="喜茶"),
                    request_evidence="分析喜茶",
                )
            ],
        )
