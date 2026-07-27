import pytest
from pydantic import ValidationError

from app.goals.schemas import GoalPlannerOutput


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


def test_clarify_and_execute_reject_respond_type() -> None:
    with pytest.raises(ValidationError):
        GoalPlannerOutput(
            action="clarify",
            question={"text": "哪个品牌？"},
            respond_type="out_of_scope",
        )


def test_unknown_respond_type_rejected() -> None:
    with pytest.raises(ValidationError):
        GoalPlannerOutput(action="respond", respond_type="chat")
