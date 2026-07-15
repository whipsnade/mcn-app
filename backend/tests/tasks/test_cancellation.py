import asyncio
from datetime import UTC, datetime

import pytest

from fakes import FakeExecutionScenario
from app.tasks.state import TaskStatus


@pytest.mark.asyncio
async def test_closing_event_stream_does_not_cancel_running_task() -> None:
    scenario = FakeExecutionScenario()
    scenario.gateway.block()
    execution = asyncio.create_task(scenario.executor.run(scenario.task.id))
    await scenario.gateway.started.wait()

    async def stream():
        yield "first-event"

    event_stream = stream()
    await anext(event_stream)
    await event_stream.aclose()
    scenario.gateway.release_success()
    await execution

    assert (await scenario.reload_task()).status == TaskStatus.COMPLETED
    assert scenario.repository.cancel_count == 0


@pytest.mark.asyncio
async def test_cancel_stops_unstarted_batches_without_cancelling_running_call() -> None:
    scenario = FakeExecutionScenario()
    scenario.task.cancel_requested_at = datetime.now(UTC).replace(tzinfo=None)

    await scenario.executor.run(scenario.task.id)

    assert scenario.task.status == TaskStatus.CANCELLED
    assert scenario.gateway.successful_logical_calls == 0
