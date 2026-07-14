import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.tasks.repository import TaskRepository
from app.tasks.schemas import TaskCreate
from app.tasks.service import TaskService
from app.tasks.state import TaskStatus
from app.workspace.models import Message
from fakes import workspace_factory_fixture as _workspace_factory_fixture  # noqa: F401


@pytest.mark.asyncio
async def test_create_task_persists_message_task_and_pending_event(
    db_session, user_factory, workspace_factory
) -> None:
    user = await user_factory()
    workspace = await workspace_factory(user.id)
    service = TaskService(db_session)

    task = await service.create(
        user.id,
        workspace.id,
        TaskCreate(content="寻找预算内的 B 站科技达人", scoring_profile="balanced"),
    )
    events = await TaskRepository(db_session).list_events_after(task.id, 0)
    messages = list(
        (await db_session.scalars(select(Message).where(Message.session_id == workspace.id))).all()
    )

    assert task.status == TaskStatus.PENDING
    assert task.user_id == user.id
    assert [(message.content, message.metadata_json) for message in messages] == [
        ("寻找预算内的 B 站科技达人", {"scoring_profile": "balanced"})
    ]
    assert [event.event_type for event in events] == ["task.pending"]


@pytest.mark.asyncio
async def test_cancel_only_marks_cancellation_requested(
    db_session, user_factory, workspace_factory
) -> None:
    user = await user_factory()
    workspace = await workspace_factory(user.id)
    service = TaskService(db_session)
    task = await service.create(user.id, workspace.id, TaskCreate(content="取消测试"))

    cancelled = await service.cancel(user.id, task.id)

    assert cancelled.cancel_requested_at is not None
    assert cancelled.status == TaskStatus.PENDING


def test_task_create_rejects_unknown_scoring_profile() -> None:
    with pytest.raises(ValidationError):
        TaskCreate(content="测试", scoring_profile="cheapest")


@pytest.mark.asyncio
async def test_task_rest_api_is_owner_scoped_and_cancel_is_request_only(
    auth_client_factory,
) -> None:
    owner = await auth_client_factory("13600000031")
    outsider = await auth_client_factory("13600000032")
    session = await owner.post(
        "/api/v1/sessions",
        json={
            "brand": "测试品牌",
            "campaign_name": "任务接口",
            "platforms": ["bilibili"],
            "category": "科技",
            "target_audience": "科技兴趣用户",
            "initial_query": "建立会话",
        },
    )
    session_id = session.json()["id"]

    created = await owner.post(
        f"/api/v1/sessions/{session_id}/tasks",
        json={"content": "寻找达人", "scoring_profile": "audience_first"},
    )

    assert created.status_code == 202
    task_id = created.json()["id"]
    assert (await owner.get(f"/api/v1/tasks/{task_id}")).status_code == 200
    assert (await outsider.get(f"/api/v1/tasks/{task_id}")).status_code == 404
    assert (await outsider.post(f"/api/v1/tasks/{task_id}/cancel")).status_code == 404
    cancelled = await owner.post(f"/api/v1/tasks/{task_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "pending"
