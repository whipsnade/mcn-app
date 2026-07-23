from contextlib import asynccontextmanager
import json

import pytest

from app.goals.context import GoalPlannerContext, GoalPlannerContextBuilder
from app.goals.planner import GoalPlannerService
from app.goals.schemas import GoalParams, GoalPlannerOutput, GoalSpec
from app.goals.validation import GoalPlanSemanticError
from app.model.contracts import StructuredResult
from app.orchestration.schemas import PlannerMessage


class FakeModel:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.requests = []

    async def complete_json(self, request):
        self.requests.append(request)
        return StructuredResult(
            value=self.outputs.pop(0),
            usage=None,
            request_id="goal-planner-test",
            regeneration_count=0,
        )


def _context() -> GoalPlannerContext:
    return GoalPlannerContext(
        user_id="user-1",
        session_id="session-1",
        task_id="task-1",
        current_message="分析喜茶 618 表现，并圈选下一轮达人",
        recent_messages=(
            PlannerMessage(
                role="user",
                content="分析喜茶 618 表现，并圈选下一轮达人",
                sequence=1,
            ),
        ),
        session_context={"active_brand": "喜茶"},
        account_default_brand=None,
        artifact_summaries=(),
    )


def _valid_output() -> GoalPlannerOutput:
    return GoalPlannerOutput(
        action="execute",
        active_brand="喜茶",
        brand_source="explicit",
        goals=[
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
        ],
    )


def _invalid_output() -> GoalPlannerOutput:
    return GoalPlannerOutput(
        action="execute",
        goals=[
            GoalSpec(
                sequence=1,
                goal_type="kol_selection",
                params=GoalParams(brand="喜茶"),
                request_evidence="用户没有说过的圈选要求",
            )
        ],
    )


@pytest.mark.asyncio
async def test_plan_task_builds_logged_structured_request() -> None:
    output = _valid_output()
    model = FakeModel([output])
    service = GoalPlannerService(model=model, context_builder=None)

    result = await service.plan_context(_context())

    assert result == output
    request = model.requests[0]
    assert request.purpose == "goal_planner"
    assert request.template_name == "goal_planner_v1"
    assert request.output_model is GoalPlannerOutput
    assert request.max_tokens == 2048
    assert request.log_context == {
        "user_id": "user-1",
        "session_id": "session-1",
        "task_id": "task-1",
        "tags": ["goal_planner:shadow", "goal_planner:attempt:1"],
    }
    payload = json.loads(request.messages[-1].content)
    assert payload["current_message"] == _context().current_message
    assert payload["account_default_brand"] is None


@pytest.mark.asyncio
async def test_semantic_invalid_output_gets_one_feedback_retry() -> None:
    valid = _valid_output()
    model = FakeModel([_invalid_output(), valid])

    result = await GoalPlannerService(model=model, context_builder=None).plan_context(_context())

    assert result == valid
    assert len(model.requests) == 2
    repair_payload = json.loads(model.requests[1].messages[-1].content)
    assert repair_payload["validation_error"] == "selection_evidence_not_in_message"
    assert model.requests[1].log_context["tags"] == [
        "goal_planner:shadow",
        "goal_planner:attempt:2",
    ]


@pytest.mark.asyncio
async def test_second_semantic_failure_is_raised_without_third_request() -> None:
    model = FakeModel([_invalid_output(), _invalid_output()])

    with pytest.raises(GoalPlanSemanticError, match="selection_evidence_not_in_message"):
        await GoalPlannerService(model=model, context_builder=None).plan_context(_context())

    assert len(model.requests) == 2


@pytest.mark.asyncio
async def test_plan_task_closes_context_session_before_model_call() -> None:
    state = {"session_open": False}

    @asynccontextmanager
    async def tracked_session():
        state["session_open"] = True
        try:
            yield object()
        finally:
            state["session_open"] = False

    class StubContextBuilder(GoalPlannerContextBuilder):
        async def _build(self, db, task_id: str) -> GoalPlannerContext:
            assert state["session_open"] is True
            assert task_id == "task-1"
            return _context()

    class SessionAwareModel(FakeModel):
        async def complete_json(self, request):
            assert state["session_open"] is False
            return await super().complete_json(request)

    model = SessionAwareModel([_valid_output()])
    service = GoalPlannerService(
        model=model,
        context_builder=StubContextBuilder(tracked_session),
    )

    result = await service.plan_task("task-1")

    assert result == _valid_output()
