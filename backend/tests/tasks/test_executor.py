import asyncio

import pytest

from fakes import FakeExecutionScenario
from app.tasks.executor import TaskExecutor, TaskRunner
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
async def test_second_executor_cannot_steal_an_active_task_lease() -> None:
    scenario = FakeExecutionScenario()
    scenario.gateway.block()
    first = asyncio.create_task(scenario.executor.run(scenario.task.id))
    await scenario.gateway.started.wait()
    second = TaskExecutor(
        repository=scenario.repository,
        context_builder=scenario,
        planner=scenario,
        gateway=scenario.gateway,
        worker_id="second-worker",
        lease_seconds=0.03,
        heartbeat_seconds=0.005,
    )

    await second.run(scenario.task.id)
    scenario.gateway.release_success()
    await first

    assert scenario.gateway.successful_logical_calls == 1
    assert scenario.task.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_runner_deduplicates_concurrent_submissions_for_one_task() -> None:
    scenario = FakeExecutionScenario()
    scenario.gateway.block()
    runner = TaskRunner(lambda: scenario.executor)

    runner.submit(scenario.task.id)
    runner.submit(scenario.task.id)
    await scenario.gateway.started.wait()
    scenario.gateway.release_success()
    await runner.shutdown()

    assert scenario.gateway.successful_logical_calls == 1


@pytest.mark.asyncio
async def test_executor_renews_lease_while_mcp_batch_is_running() -> None:
    scenario = FakeExecutionScenario()
    scenario.gateway.block()
    execution = asyncio.create_task(scenario.executor.run(scenario.task.id))
    await scenario.gateway.started.wait()

    await asyncio.sleep(0.02)
    scenario.gateway.release_success()
    await execution

    assert scenario.repository.renew_count >= 1


@pytest.mark.asyncio
async def test_non_terminal_mcp_call_never_marks_task_completed() -> None:
    scenario = FakeExecutionScenario()
    scenario.gateway.outcome_status = "running"

    await scenario.executor.run(scenario.task.id)

    assert scenario.task.status == TaskStatus.INTERRUPTED


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["released", "failed"])
async def test_known_failed_mcp_outcome_marks_task_failed(status: str) -> None:
    scenario = FakeExecutionScenario()
    scenario.gateway.outcome_status = status

    await scenario.executor.run(scenario.task.id)

    assert scenario.task.status == TaskStatus.FAILED


@pytest.mark.asyncio
async def test_unexpected_executor_error_marks_task_failed() -> None:
    scenario = FakeExecutionScenario()
    scenario.gateway.error = RuntimeError("upstream exploded")

    await scenario.executor.run(scenario.task.id)

    assert scenario.task.status == TaskStatus.FAILED


@pytest.mark.asyncio
async def test_executor_uses_persisted_plan_without_replanning() -> None:
    scenario = FakeExecutionScenario()
    scenario.task.plan_json = scenario.plan.model_dump(mode="json")
    scenario.task.status = TaskStatus.INTERRUPTED

    await scenario.executor.run(scenario.task.id)

    assert scenario.task.status == TaskStatus.COMPLETED
    assert scenario.gateway.successful_logical_calls == 1
