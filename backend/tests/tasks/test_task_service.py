import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.identity.dependencies import get_function_scoped_current_user
from app.main import create_app
from app.tasks.models import AnalysisTask, TaskEvent
from app.tasks.repository import TaskRepository
from app.tasks.schemas import TaskCreate
from app.tasks.service import TaskService
from app.tasks.state import TaskStatus
from app.workspace.models import Message
from fakes import (
    CommitFailingSession,
    workspace_factory_fixture as _workspace_factory_fixture,  # noqa: F401
)


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


@pytest.mark.asyncio
async def test_cancel_is_idempotent(db_session, user_factory, workspace_factory) -> None:
    user = await user_factory()
    workspace = await workspace_factory(user.id)
    service = TaskService(db_session)
    task = await service.create(user.id, workspace.id, TaskCreate(content="幂等取消"))
    first = await service.cancel(user.id, task.id)
    first_requested_at = first.cancel_requested_at
    first_updated_at = first.updated_at
    await asyncio.sleep(0.001)

    second = await service.cancel(user.id, task.id)

    assert second.cancel_requested_at == first_requested_at
    assert second.updated_at == first_updated_at


def test_task_create_rejects_unknown_scoring_profile() -> None:
    with pytest.raises(ValidationError):
        TaskCreate(content="测试", scoring_profile="cheapest")


@pytest.mark.parametrize("content", ["   ", "\t", "\n \t"])
def test_task_create_rejects_whitespace_only_content(content: str) -> None:
    with pytest.raises(ValidationError):
        TaskCreate(content=content)


def test_task_create_preserves_nonblank_content_whitespace() -> None:
    assert TaskCreate(content="  保留原文  ").content == "  保留原文  "


def test_message_content_metadata_uses_mysql_mediumtext() -> None:
    assert isinstance(Message.__table__.c.content.type, MEDIUMTEXT)


@pytest.mark.asyncio
async def test_message_content_database_column_is_mediumtext(db_session) -> None:
    data_type = await db_session.scalar(
        text(
            "SELECT DATA_TYPE FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() "
            "AND TABLE_NAME = 'messages' AND COLUMN_NAME = 'content'"
        )
    )
    assert data_type == "mediumtext"


@pytest.mark.asyncio
async def test_create_task_persists_20000_four_byte_characters(
    db_session, user_factory, workspace_factory
) -> None:
    user = await user_factory()
    workspace = await workspace_factory(user.id)
    content = "😀" * 20_000

    task = await TaskService(db_session).create(
        user.id, workspace.id, TaskCreate(content=content)
    )
    message = await db_session.get(Message, task.trigger_message_id)

    assert message is not None
    assert message.content == content


def test_task_create_rejects_20001_characters() -> None:
    with pytest.raises(ValidationError):
        TaskCreate(content="😀" * 20_001)


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


@pytest.mark.asyncio
async def test_create_api_commit_failure_is_not_reported_as_accepted_and_is_atomic(
    db_session, user_factory, workspace_factory
) -> None:
    user = await user_factory()
    workspace = await workspace_factory(user.id)
    workspace_id = workspace.id
    await db_session.commit()
    failing_db = CommitFailingSession(db_session)
    app = create_app()

    async def override_get_db():
        try:
            yield failing_db
            await failing_db.commit()
        except Exception:
            await failing_db.rollback()
            raise

    async def override_current_user():
        return user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_function_scoped_current_user] = override_current_user
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/api/v1/sessions/{workspace_id}/tasks",
            json={"content": "提交必须成功后再响应"},
        )

    assert response.status_code >= 400
    async with AsyncSession(bind=db_session.bind, expire_on_commit=False) as verification:
        message_count = await verification.scalar(
            select(func.count(Message.id)).where(Message.session_id == workspace_id)
        )
        task_count = await verification.scalar(
            select(func.count(AnalysisTask.id)).where(AnalysisTask.session_id == workspace_id)
        )
        event_count = await verification.scalar(
            select(func.count(TaskEvent.id))
            .join(AnalysisTask, TaskEvent.task_id == AnalysisTask.id)
            .where(AnalysisTask.session_id == workspace_id)
        )
    assert (message_count, task_count, event_count) == (0, 0, 0)


@pytest.mark.asyncio
async def test_cancel_api_commit_failure_is_not_reported_as_success(
    db_session, user_factory, workspace_factory
) -> None:
    user = await user_factory()
    workspace = await workspace_factory(user.id)
    task = await TaskService(db_session).create(
        user.id, workspace.id, TaskCreate(content="取消提交失败")
    )
    task_id = task.id
    await db_session.commit()
    failing_db = CommitFailingSession(db_session)
    app = create_app()

    async def override_get_db():
        try:
            yield failing_db
            await failing_db.commit()
        except Exception:
            await failing_db.rollback()
            raise

    async def override_current_user():
        return user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_function_scoped_current_user] = override_current_user
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.post(f"/api/v1/tasks/{task_id}/cancel")

    assert response.status_code >= 400
    async with AsyncSession(bind=db_session.bind, expire_on_commit=False) as verification:
        persisted = await verification.get(AnalysisTask, task_id)
        assert persisted is not None
        assert persisted.cancel_requested_at is None
