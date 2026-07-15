import pytest

from fakes import FakeExecutionScenario
from app.tasks.state import TaskStatus


@pytest.mark.asyncio
async def test_executor_persists_plan_executes_each_step_once_and_completes() -> None:
    scenario = FakeExecutionScenario()

    await scenario.executor.run(scenario.task.id)

    assert scenario.task.status == TaskStatus.COMPLETED
    assert scenario.task.plan_json is not None
    assert scenario.gateway.successful_logical_calls == 1
    assert await scenario.wallet_tuple() == (990, 0)


@pytest.mark.asyncio
async def test_executor_uses_persisted_plan_without_replanning() -> None:
    scenario = FakeExecutionScenario()
    scenario.task.plan_json = scenario.plan.model_dump(mode="json")
    scenario.task.status = TaskStatus.INTERRUPTED

    await scenario.executor.run(scenario.task.id)

    assert scenario.task.status == TaskStatus.COMPLETED
    assert scenario.gateway.successful_logical_calls == 1
