from contextlib import asynccontextmanager

import pytest

from app.goals.context import GoalPlannerContextBuilder
from app.tasks.schemas import TaskCreate
from app.tasks.service import TaskService
from app.workspace.schemas import MessageCreate, SessionCreate
from app.workspace.service import WorkspaceService


@pytest.mark.asyncio
async def test_context_uses_trigger_message_and_session_brand(db_session, user_factory) -> None:
    user = await user_factory()
    workspace = await WorkspaceService(db_session).create_session(
        user.id,
        SessionCreate(brand="喜茶", category="茶饮"),
    )
    task = await TaskService(db_session).create(
        user.id,
        workspace.id,
        TaskCreate(content="分析 618 活动表现"),
    )

    @asynccontextmanager
    async def borrowed_session():
        yield db_session

    context = await GoalPlannerContextBuilder(borrowed_session).build(task.id)

    assert context.task_id == task.id
    assert context.current_message == "分析 618 活动表现"
    assert context.session_context["active_brand"] == "喜茶"
    assert context.session_context["category"] == "茶饮"
    assert context.account_default_brand is None
    assert context.allowed_goal_types == (
        "brand_analysis",
        "campaign_analysis",
        "kol_selection",
    )
    assert context.recent_messages[-1].content == "分析 618 活动表现"


@pytest.mark.asyncio
async def test_context_excludes_messages_after_trigger(db_session, user_factory) -> None:
    user = await user_factory()
    workspace_service = WorkspaceService(db_session)
    workspace = await workspace_service.create_session(
        user.id,
        SessionCreate(brand="喜茶", category="茶饮"),
    )
    task = await TaskService(db_session).create(
        user.id,
        workspace.id,
        TaskCreate(content="分析 618 活动表现"),
    )
    await workspace_service.append_message(
        user.id,
        workspace.id,
        MessageCreate(content="这条消息发生在任务触发之后"),
    )

    @asynccontextmanager
    async def borrowed_session():
        yield db_session

    context = await GoalPlannerContextBuilder(borrowed_session).build(task.id)

    assert [message.content for message in context.recent_messages] == ["分析 618 活动表现"]
