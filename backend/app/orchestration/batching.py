from __future__ import annotations

from dataclasses import dataclass

from app.orchestration.schemas import PlanValidationError, ToolPlan, ToolPlanStep


@dataclass(frozen=True)
class ExecutionBatch:
    steps: tuple[ToolPlanStep, ...]


def _step_sort_key(step_id: str) -> tuple[int, str]:
    return (int(step_id.removeprefix("step_")), step_id)


def build_execution_batches(plan: ToolPlan) -> tuple[ExecutionBatch, ...]:
    """按 DAG 层级产生确定性的、可并发执行的工具批次。"""
    steps_by_id = {step.id: step for step in plan.steps}
    if len(steps_by_id) != len(plan.steps):
        raise PlanValidationError("PLAN_DUPLICATE_STEP_ID")

    dependencies = {step.id: set(step.depends_on) for step in plan.steps}
    for step_id, required in dependencies.items():
        if not required.issubset(steps_by_id):
            raise PlanValidationError("PLAN_DEPENDENCY_UNKNOWN")

    resolved: set[str] = set()
    batches: list[ExecutionBatch] = []
    while len(resolved) < len(steps_by_id):
        ready_ids = sorted(
            (step_id for step_id, required in dependencies.items() if step_id not in resolved and required <= resolved),
            key=_step_sort_key,
        )
        if not ready_ids:
            raise PlanValidationError("PLAN_DEPENDENCY_CYCLE")
        batches.append(ExecutionBatch(steps=tuple(steps_by_id[step_id] for step_id in ready_ids)))
        resolved.update(ready_ids)
    return tuple(batches)
