from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.identity.models import User
from app.tasks.models import AnalysisTask
from app.workspace.models import Message, WorkspaceSession


async def create_analysis_task(
    db_session: AsyncSession,
    user_factory: Callable[[], Awaitable[User]],
) -> AnalysisTask:
    user = await user_factory()
    now = datetime.now(UTC).replace(tzinfo=None)
    workspace = WorkspaceSession(
        id=str(uuid4()),
        user_id=user.id,
        title="Task 5 test",
        brand="测试品牌",
        campaign_name="测试活动",
        status="draft",
        platforms=["bilibili"],
        category="测试",
        target_audience="测试人群",
        budget_min=Decimal("1.00"),
        budget_max=Decimal("2.00"),
        filters_snapshot={},
        is_starred=False,
        last_accessed_at=now,
        created_at=now,
        updated_at=now,
    )
    message = Message(
        id=str(uuid4()),
        session_id=workspace.id,
        user_id=user.id,
        role="user",
        content="测试",
        sequence=1,
        metadata_json={},
        created_at=now,
    )
    task = AnalysisTask(
        id=str(uuid4()),
        user_id=user.id,
        session_id=workspace.id,
        trigger_message_id=message.id,
        status="running",
        plan_json={},
        plan_version="v1",
        max_calls=10,
        estimated_points=10,
        created_at=now,
        updated_at=now,
    )
    db_session.add(workspace)
    await db_session.flush()
    db_session.add(message)
    await db_session.flush()
    db_session.add(task)
    await db_session.flush()
    return task


def strict_object_schema(properties: dict[str, Any], *required: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }
