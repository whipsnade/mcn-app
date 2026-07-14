import asyncio

import pytest

from app.tasks.models import TaskEvent
from fakes import (
    collect_event_ids,
    persisted_task_fixture as _persisted_task_fixture,  # noqa: F401
    task_event_stream_fixture as _task_event_stream_fixture,  # noqa: F401
    workspace_factory_fixture as _workspace_factory_fixture,  # noqa: F401
)


@pytest.mark.asyncio
async def test_stream_subscribes_before_replay_and_deduplicates(
    task_event_stream, persisted_task, monkeypatch
) -> None:
    first = await task_event_stream.append(persisted_task, "plan.ready", {"calls": 1})
    replay_started = asyncio.Event()
    continue_replay = asyncio.Event()
    original = task_event_stream.repository.list_events_after

    async def gated(*args, **kwargs):
        rows = await original(*args, **kwargs)
        replay_started.set()
        await continue_replay.wait()
        return rows

    monkeypatch.setattr(task_event_stream.repository, "list_events_after", gated)
    collection = asyncio.create_task(
        collect_event_ids(
            task_event_stream.stream(persisted_task.id, persisted_task.user_id, 0),
            count=2,
        )
    )
    await replay_started.wait()
    second = await task_event_stream.append(persisted_task, "tool.started", {"call_id": "c1"})
    continue_replay.set()

    assert await collection == [first.id, second.id]


def test_sse_event_contains_id_event_and_json_data() -> None:
    from app.tasks.router import encode_sse_event

    event = TaskEvent(
        id=7,
        task_id="task-1",
        user_id="user-1",
        event_type="plan.ready",
        payload_json={"calls": 1},
    )

    assert encode_sse_event(event) == (
        'id: 7\nevent: plan.ready\ndata: {"calls":1}\n\n'
    )


def test_last_event_id_header_takes_precedence_over_query() -> None:
    from app.tasks.router import resolve_last_event_id

    assert resolve_last_event_id("11", 4) == 11
    assert resolve_last_event_id(None, 4) == 4


@pytest.mark.asyncio
async def test_heartbeat_and_disconnect_only_unsubscribe(
    db_session, task_event_stream, persisted_task
) -> None:
    from app.tasks.router import sse_event_chunks

    chunks = sse_event_chunks(
        task_event_stream.stream(persisted_task.id, persisted_task.user_id, 0),
        heartbeat_seconds=0.001,
    )

    assert await asyncio.wait_for(anext(chunks), timeout=1) == ": heartbeat\n\n"
    assert persisted_task.id in task_event_stream.broker.subscriptions
    await chunks.aclose()
    await asyncio.sleep(0)

    await db_session.refresh(persisted_task)
    assert task_event_stream.broker.subscriptions == {}
    assert persisted_task.status == "pending"
    assert persisted_task.cancel_requested_at is None


@pytest.mark.asyncio
async def test_sse_checks_ownership_before_starting_stream(auth_client_factory) -> None:
    owner = await auth_client_factory("13600000041")
    outsider = await auth_client_factory("13600000042")
    session = await owner.post(
        "/api/v1/sessions",
        json={
            "brand": "测试品牌",
            "campaign_name": "SSE 所有权",
            "platforms": ["bilibili"],
            "category": "科技",
            "target_audience": "科技兴趣用户",
            "initial_query": "建立会话",
        },
    )
    created = await owner.post(
        f"/api/v1/sessions/{session.json()['id']}/tasks",
        json={"content": "开始分析"},
    )

    response = await outsider.get(f"/api/v1/tasks/{created.json()['id']}/events")

    assert response.status_code == 404
    assert response.json() == {"detail": "task_not_found"}
