import asyncio

import pytest

from fakes import FakeExecutionScenario, InjectedProcessCrash
from app.tasks.state import TaskStatus


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "crash_at",
    ["after_reserve", "after_mcp_result", "after_settle", "after_candidates", "after_bi"],
)
async def test_recovery_reuses_persisted_success(crash_at: str) -> None:
    scenario = FakeExecutionScenario(crash_at=crash_at)
    with pytest.raises(InjectedProcessCrash):
        await scenario.executor.run(scenario.task.id)

    scenario.crash_at = None
    await scenario.new_recovery().recover_expired()
    for _ in range(10):
        if scenario.task.status == TaskStatus.COMPLETED:
            break
        await asyncio.sleep(0)

    assert (await scenario.reload_task()).status == TaskStatus.COMPLETED
    assert scenario.gateway.successful_logical_calls == 1
    assert await scenario.wallet_tuple() == (990, 0)


@pytest.mark.asyncio
async def test_recovery_submits_to_runner_instead_of_executing_inline() -> None:
    scenario = FakeExecutionScenario()
    scenario.task.status = TaskStatus.INTERRUPTED
    recovery = scenario.new_recovery()

    await recovery.recover_expired()

    assert scenario.recovery_runner.submitted == [scenario.task.id]
