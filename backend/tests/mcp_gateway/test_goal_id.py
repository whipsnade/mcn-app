from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.mcp_gateway.contracts import DataTapService
from app.mcp_gateway.models import McpCall
from app.mcp_gateway.registry import ApprovedTool, ToolRegistryService
from app.mcp_gateway.service import McpCallService
from app.mcp_gateway.transport import LogicalCallConflictError
from app.tasks.models import AnalysisTask
from app.workspace.models import Message, WorkspaceSession


class _NoopTransport:
    def protocol_session_digest(self, service):
        return None


class _NoopArgumentsLoader:
    async def load_arguments(self, *, task_id: str, plan_step_id: str):
        return {}


_FAKE_TOOL = ApprovedTool(
    catalog_id="catalog-1",
    internal_name="datatap.fake.kol.search.v1",
    service=DataTapService.INSIGHT_CUBE,
    remote_name="fake_search",
    input_schema={
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "additionalProperties": False,
    },
    output_schema={"type": "object"},
)


async def _create_task(db_session, user_factory) -> tuple[str, str]:
    user = await user_factory()
    now = datetime.now(UTC).replace(tzinfo=None)
    session = WorkspaceSession(
        id=str(uuid4()),
        user_id=user.id,
        title="goal_id 测试会话",
        brand="",
        campaign_name=None,
        status="active",
        platforms=["xiaohongshu"],
        category="美食",
        target_audience="",
        budget_min=None,
        budget_max=None,
        filters_snapshot={},
        is_starred=False,
        last_accessed_at=now,
        created_at=now,
        updated_at=now,
    )
    db_session.add(session)
    await db_session.flush()
    message = Message(
        id=str(uuid4()),
        session_id=session.id,
        user_id=user.id,
        role="user",
        content="圈选美食达人",
        sequence=1,
        metadata_json={},
        created_at=now,
    )
    db_session.add(message)
    await db_session.flush()
    task = AnalysisTask(
        id=str(uuid4()),
        user_id=user.id,
        session_id=session.id,
        trigger_message_id=message.id,
        kind="agent",
        status="queued",
        max_calls=10,
        estimated_points=0,
        creation_order=1,
        created_at=now,
        updated_at=now,
    )
    db_session.add(task)
    await db_session.flush()
    return user.id, task.id


def _patch_registry(monkeypatch) -> None:
    async def fake_require_enabled(self, internal_tool_name: str) -> ApprovedTool:
        return _FAKE_TOOL

    monkeypatch.setattr(ToolRegistryService, "require_enabled", fake_require_enabled)


def _service(db_session) -> McpCallService:
    return McpCallService(
        db_session,
        _NoopTransport(),
        arguments_loader=_NoopArgumentsLoader(),
    )


@pytest.mark.asyncio
async def test_prepare_persists_goal_id(db_session, user_factory, monkeypatch) -> None:
    user_id, task_id = await _create_task(db_session, user_factory)
    _patch_registry(monkeypatch)

    row = await _service(db_session).prepare(
        logical_call_id=str(uuid4()),
        user_id=user_id,
        task_id=task_id,
        plan_step_id="step_1",
        internal_tool_name=_FAKE_TOOL.internal_name,
        arguments={"name": "美食"},
        goal_id="goal-1",
    )

    assert row.goal_id == "goal-1"


@pytest.mark.asyncio
async def test_prepare_defaults_goal_id_to_none(db_session, user_factory, monkeypatch) -> None:
    user_id, task_id = await _create_task(db_session, user_factory)
    _patch_registry(monkeypatch)

    row = await _service(db_session).prepare(
        logical_call_id=str(uuid4()),
        user_id=user_id,
        task_id=task_id,
        plan_step_id="step_1",
        internal_tool_name=_FAKE_TOOL.internal_name,
        arguments={"name": "美食"},
    )

    assert row.goal_id is None


@pytest.mark.asyncio
async def test_prepare_replay_with_same_goal_id_is_idempotent(
    db_session, user_factory, monkeypatch
) -> None:
    user_id, task_id = await _create_task(db_session, user_factory)
    _patch_registry(monkeypatch)
    service = _service(db_session)
    logical_call_id = str(uuid4())

    first = await service.prepare(
        logical_call_id=logical_call_id,
        user_id=user_id,
        task_id=task_id,
        plan_step_id="step_1",
        internal_tool_name=_FAKE_TOOL.internal_name,
        arguments={"name": "美食"},
        goal_id="goal-1",
    )
    replay = await service.prepare(
        logical_call_id=logical_call_id,
        user_id=user_id,
        task_id=task_id,
        plan_step_id="step_1",
        internal_tool_name=_FAKE_TOOL.internal_name,
        arguments={"name": "美食"},
        goal_id="goal-1",
    )

    assert replay.id == first.id
    total = await db_session.scalar(
        select(func.count()).select_from(McpCall).where(
            McpCall.logical_call_id == logical_call_id
        )
    )
    assert total == 1


@pytest.mark.asyncio
async def test_prepare_replay_with_different_goal_id_conflicts(
    db_session, user_factory, monkeypatch
) -> None:
    user_id, task_id = await _create_task(db_session, user_factory)
    _patch_registry(monkeypatch)
    service = _service(db_session)
    logical_call_id = str(uuid4())

    await service.prepare(
        logical_call_id=logical_call_id,
        user_id=user_id,
        task_id=task_id,
        plan_step_id="step_1",
        internal_tool_name=_FAKE_TOOL.internal_name,
        arguments={"name": "美食"},
        goal_id="goal-1",
    )
    with pytest.raises(LogicalCallConflictError):
        await service.prepare(
            logical_call_id=logical_call_id,
            user_id=user_id,
            task_id=task_id,
            plan_step_id="step_1",
            internal_tool_name=_FAKE_TOOL.internal_name,
            arguments={"name": "美食"},
            goal_id="goal-2",
        )
