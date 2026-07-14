import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.core.security import create_access_token
from app.db.session import SessionFactory, get_db
from app.identity.dependencies import get_function_scoped_current_user
from app.identity.models import LoginSession, User
from app.main import create_app
from app.tasks.models import AnalysisTask
from app.tasks.models import TaskEvent
from app.tasks.state import TaskStatus
from app.workspace.models import Message, WorkspaceSession
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

    assert resolve_last_event_id("11", "4") == 11
    assert resolve_last_event_id(None, "4") == 4
    assert resolve_last_event_id(None, None) == 0


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


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_query", ["invalid", "-9"])
async def test_last_event_id_valid_header_ignores_invalid_query(
    invalid_query, db_session, persisted_task
) -> None:
    from app.tasks.router import get_task_event_stream

    user = await db_session.get(User, persisted_task.user_id)
    seen: list[int] = []

    class FiniteTaskEventStream:
        async def stream(self, task_id: str, user_id: str, last_event_id: int):
            seen.append(last_event_id)
            if False:
                yield None

    async def override_get_db():
        yield db_session

    async def override_current_user():
        return user

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_function_scoped_current_user] = override_current_user
    app.dependency_overrides[get_task_event_stream] = FiniteTaskEventStream
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/api/v1/tasks/{persisted_task.id}/events",
            params={"last_event_id": invalid_query},
            headers={"Last-Event-ID": "11"},
        )

    assert response.status_code == 200
    assert seen == [11]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("header_value", "query_value"),
    [("invalid", "4"), ("-1", "4"), (None, "invalid"), (None, "-1")],
)
async def test_last_event_id_rejects_invalid_selected_value(
    header_value, query_value, db_session, persisted_task
) -> None:
    from app.tasks.router import get_task_event_stream

    user = await db_session.get(User, persisted_task.user_id)

    class FiniteTaskEventStream:
        async def stream(self, task_id: str, user_id: str, last_event_id: int):
            if False:
                yield None

    async def override_get_db():
        yield db_session

    async def override_current_user():
        return user

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_function_scoped_current_user] = override_current_user
    app.dependency_overrides[get_task_event_stream] = FiniteTaskEventStream
    headers = {"Last-Event-ID": header_value} if header_value is not None else {}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/api/v1/tasks/{persisted_task.id}/events",
            params={"last_event_id": query_value},
            headers=headers,
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_sse_closes_auth_and_ownership_session_before_first_frame() -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    user_id = str(uuid4())
    login_id = str(uuid4())
    workspace_id = str(uuid4())
    message_id = str(uuid4())
    task_id = str(uuid4())
    async with SessionFactory.begin() as setup:
        setup.add(
            User(
                id=user_id,
                nickname="SSE 生命周期",
                role="user",
                status="active",
                created_at=now,
                updated_at=now,
            )
        )
        await setup.flush()
        setup.add(
            LoginSession(
                id=login_id,
                user_id=user_id,
                refresh_token_hash=uuid4().hex,
                expires_at=now + timedelta(days=1),
                revoked_at=None,
                created_at=now,
                last_seen_at=now,
            )
        )
        setup.add(
            WorkspaceSession(
                id=workspace_id,
                user_id=user_id,
                title="SSE 生命周期",
                brand="测试品牌",
                campaign_name="SSE 生命周期",
                status="draft",
                platforms=["bilibili"],
                category="科技",
                target_audience="科技兴趣用户",
                budget_min=None,
                budget_max=None,
                filters_snapshot={},
                is_starred=False,
                last_accessed_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        await setup.flush()
        setup.add(
            Message(
                id=message_id,
                session_id=workspace_id,
                user_id=user_id,
                role="user",
                content="SSE 生命周期",
                sequence=1,
                metadata_json={},
                created_at=now,
            )
        )
        await setup.flush()
        setup.add(
            AnalysisTask(
                id=task_id,
                user_id=user_id,
                session_id=workspace_id,
                trigger_message_id=message_id,
                status=TaskStatus.PENDING,
                plan_json=None,
                plan_version=None,
                max_calls=10,
                estimated_points=0,
                error_code=None,
                error_message=None,
                cancel_requested_at=None,
                lease_owner=None,
                lease_expires_at=None,
                started_at=None,
                completed_at=None,
                created_at=now,
                updated_at=now,
            )
        )
        await setup.flush()
        setup.add(
            TaskEvent(
                task_id=task_id,
                user_id=user_id,
                event_type="task.pending",
                payload_json={"status": "pending"},
                created_at=now,
            )
        )

    lifecycle: list[dict[str, bool]] = []

    async def observed_get_db():
        record = {"closed": False}
        lifecycle.append(record)
        try:
            async with SessionFactory() as session:
                try:
                    yield session
                    await session.commit()
                except Exception:
                    await session.rollback()
                    raise
        finally:
            record["closed"] = True

    app = create_app()
    app.dependency_overrides[get_db] = observed_get_db
    token = create_access_token(user_id=user_id, session_id=login_id, role="user")
    request_sent = False
    disconnected = asyncio.Event()
    first_frame = b""
    preflight_closed_at_first_frame = False

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        nonlocal first_frame, preflight_closed_at_first_frame
        if message["type"] == "http.response.body" and message.get("body"):
            first_frame = message["body"]
            preflight_closed_at_first_frame = bool(lifecycle) and all(
                item["closed"] for item in lifecycle
            )
            disconnected.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": f"/api/v1/tasks/{task_id}/events",
        "raw_path": f"/api/v1/tasks/{task_id}/events".encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
        "state": {},
    }
    try:
        await asyncio.wait_for(app(scope, receive, send), timeout=2)
    finally:
        async with SessionFactory.begin() as cleanup:
            await cleanup.execute(delete(TaskEvent).where(TaskEvent.task_id == task_id))
            await cleanup.execute(delete(AnalysisTask).where(AnalysisTask.id == task_id))
            await cleanup.execute(delete(Message).where(Message.id == message_id))
            await cleanup.execute(
                delete(WorkspaceSession).where(WorkspaceSession.id == workspace_id)
            )
            await cleanup.execute(delete(LoginSession).where(LoginSession.id == login_id))
            await cleanup.execute(delete(User).where(User.id == user_id))

    assert b"event: task.pending" in first_frame
    assert preflight_closed_at_first_frame is True
