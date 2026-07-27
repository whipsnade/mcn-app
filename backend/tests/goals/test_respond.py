import pytest
from pydantic import ValidationError

from app.goals.schemas import GoalParams, GoalPlannerOutput, GoalSpec
from app.goals.validation import validate_goal_plan


def test_respond_requires_respond_type_without_goals_or_question() -> None:
    output = GoalPlannerOutput(action="respond", respond_type="context_qa")
    assert output.respond_type == "context_qa"

    with pytest.raises(ValidationError):
        GoalPlannerOutput(action="respond")
    with pytest.raises(ValidationError):
        GoalPlannerOutput(
            action="respond",
            respond_type="usage_help",
            question={"text": "哪个品牌？"},
        )


def test_non_respond_actions_reject_respond_type() -> None:
    with pytest.raises(ValidationError):
        GoalPlannerOutput(
            action="clarify",
            question={"text": "哪个品牌？"},
            respond_type="out_of_scope",
        )
    with pytest.raises(ValidationError):
        GoalPlannerOutput(
            action="execute",
            respond_type="context_qa",
            goals=[
                GoalSpec(
                    sequence=1,
                    goal_type="campaign_analysis",
                    params=GoalParams(brand="喜茶", campaign="618"),
                    request_evidence="分析喜茶 618 表现",
                )
            ],
        )


def test_respond_rejects_goals() -> None:
    with pytest.raises(ValidationError):
        GoalPlannerOutput(
            action="respond",
            respond_type="context_qa",
            goals=[
                GoalSpec(
                    sequence=1,
                    goal_type="campaign_analysis",
                    params=GoalParams(brand="喜茶", campaign="618"),
                    request_evidence="分析喜茶 618 表现",
                )
            ],
        )


def test_validate_goal_plan_skips_all_checks_for_respond() -> None:
    # 会话已有品牌时，respond 不得触发 brand_source_context_mismatch。
    output = GoalPlannerOutput(action="respond", respond_type="context_qa")
    validate_goal_plan(
        output,
        "为什么上次分析失败了？",
        session_brand="海底捞",
        account_default_brand="喜茶",
    )


def test_unknown_respond_type_rejected() -> None:
    with pytest.raises(ValidationError):
        GoalPlannerOutput(action="respond", respond_type="chat")
