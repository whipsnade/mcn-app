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

    assert (await scenario.reload_task()).status == TaskStatus.COMPLETED
    assert scenario.gateway.successful_logical_calls == 1
    assert await scenario.wallet_tuple() == (990, 0)
