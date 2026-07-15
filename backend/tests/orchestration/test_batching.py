from __future__ import annotations

import pytest

from app.orchestration.batching import build_execution_batches
from app.orchestration.planner import PlanValidationError
from app.orchestration.schemas import ToolPlan, ToolPlanStep


def _step(step_id: str, *, depends_on: tuple[str, ...] = ()) -> ToolPlanStep:
    return ToolPlanStep(
        id=step_id,
        internal_tool_name="kol.search",
        arguments={"keyword": "美妆"},
        depends_on=depends_on,
        evidence_goal="候选达人列表",
    )


def test_build_execution_batches_uses_stable_topological_order() -> None:
    plan = ToolPlan(
        objective="寻找合适达人",
        steps=(
            _step("step_3", depends_on=("step_1",)),
            _step("step_2"),
            _step("step_1"),
            _step("step_4", depends_on=("step_1", "step_2")),
        ),
    )

    batches = build_execution_batches(plan)

    assert [[step.id for step in batch.steps] for batch in batches] == [
        ["step_1", "step_2"],
        ["step_3", "step_4"],
    ]


def test_build_execution_batches_rejects_dependency_cycle() -> None:
    plan = ToolPlan(
        objective="寻找合适达人",
        steps=(
            _step("step_1", depends_on=("step_2",)),
            _step("step_2", depends_on=("step_1",)),
        ),
    )

    with pytest.raises(PlanValidationError) as caught:
        build_execution_batches(plan)

    assert caught.value.code == "PLAN_DEPENDENCY_CYCLE"


def test_build_execution_batches_rejects_missing_dependency() -> None:
    plan = ToolPlan(
        objective="寻找合适达人",
        steps=(_step("step_1", depends_on=("step_404",)),),
    )

    with pytest.raises(PlanValidationError) as caught:
        build_execution_batches(plan)

    assert caught.value.code == "PLAN_DEPENDENCY_UNKNOWN"
