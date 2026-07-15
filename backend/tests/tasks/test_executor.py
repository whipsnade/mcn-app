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
async def test_lost_lease_during_slow_planning_never_enters_mcp_gateway() -> None:
    scenario = FakeExecutionScenario()
    scenario.repository.allow_renew = False
    scenario.block_context()
    old_execution = asyncio.create_task(scenario.executor.run(scenario.task.id))
    await scenario.context_started.wait()

    await asyncio.sleep(0.04)
    claimed = await scenario.repository.claim_lease(scenario.task.id, "new-worker", 1)
    assert claimed is not None
    scenario.release_context()
    await old_execution

    assert scenario.gateway.successful_logical_calls == 0


@pytest.mark.asyncio
async def test_lease_stolen_between_executor_check_and_gateway_never_calls_outbound() -> None:
    scenario = FakeExecutionScenario()
    scenario.gateway.before_lease_guard = scenario.steal_lease_before_gateway

    await scenario.executor.run(scenario.task.id)

    assert scenario.gateway.successful_logical_calls == 0


@pytest.mark.asyncio
async def test_heartbeat_renew_error_fails_closed_before_gateway() -> None:
    scenario = FakeExecutionScenario()
    scenario.repository.renew_error = RuntimeError("renew unavailable")
    scenario.block_context()
    execution = asyncio.create_task(scenario.executor.run(scenario.task.id))
    await scenario.context_started.wait()
    await asyncio.sleep(0.01)
    scenario.release_context()
    await execution

    assert scenario.gateway.successful_logical_calls == 0


def test_default_heartbeat_is_strictly_shorter_than_one_second_lease() -> None:
    scenario = FakeExecutionScenario()
    executor = TaskExecutor(
        repository=scenario.repository,
        context_builder=scenario,
        planner=scenario,
        gateway=scenario.gateway,
        worker_id="one-second-worker",
        lease_seconds=1,
    )

    assert 0 < executor.heartbeat_seconds < 1


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


@pytest.mark.asyncio
async def test_lost_lease_after_mcp_never_starts_artifact_writes() -> None:
    scenario = FakeExecutionScenario()
    calls: list[str] = []

    class Artifacts:
        async def build_candidates(self, task_id: str) -> None:
            calls.append("candidates")

        async def build_bi_report(self, task_id: str) -> None:
            calls.append("bi")

        async def stream_summary(self, task_id: str) -> None:
            calls.append("summary")

    async def lose_lease(name: str) -> None:
        if name == "after_mcp_result":
            scenario.repository.task.lease_owner = "replacement-worker"

    scenario.executor.artifacts = Artifacts()
    scenario.executor.checkpoint = lose_lease
    await scenario.executor.run(scenario.task.id)

    assert calls == []
