"""release_expired_unknown：恢复协调置任务 FAILED 时级联转变非终态 task_goals。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.billing.models import Wallet
from app.goals.models import TaskGoal
from app.identity.models import User
from app.mcp_gateway.models import McpCall
from app.tasks.models import AnalysisTask, TaskEvent
from app.tasks.repository import TaskRepository
from app.workspace.models import Message, WorkspaceSession


async def _seed_task_with_goals(
    db_session,
    *,
    with_goals: bool = True,
    unknown_completed_at: datetime | None = None,
    cancel_requested: bool = False,
    status: str = "running",
) -> dict[str, str]:
    now = datetime.now(UTC).replace(tzinfo=None)
    ids = {
        "user_id": str(uuid4()),
        "session_id": str(uuid4()),
        "message_id": str(uuid4()),
        "task_id": str(uuid4()),
    }
    user = User(
        id=ids["user_id"],
        nickname="恢复协调测试",
        role="user",
        status="active",
        created_at=now,
        updated_at=now,
    )
    db_session.add(user)
    await db_session.flush()
    db_session.add(
        Wallet(user_id=user.id, balance=990, reserved=10, version=1, updated_at=now)
    )
    db_session.add(
        WorkspaceSession(
            id=ids["session_id"],
            user_id=user.id,
            title="恢复协调测试会话",
            brand="海底捞",
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
    )
    await db_session.flush()
    db_session.add(
        Message(
            id=ids["message_id"],
            session_id=ids["session_id"],
            user_id=ids["user_id"],
            role="user",
            content="分析海底捞并圈选达人",
            sequence=1,
            metadata_json={},
            created_at=now,
        )
    )
    await db_session.flush()
    db_session.add(
        AnalysisTask(
            id=ids["task_id"],
            user_id=ids["user_id"],
            session_id=ids["session_id"],
            trigger_message_id=ids["message_id"],
            status=status,
            kind="agent",
            lease_owner="dead-worker",
            lease_expires_at=now - timedelta(minutes=10),
            cancel_requested_at=now - timedelta(minutes=5) if cancel_requested else None,
            started_at=now - timedelta(hours=1),
            max_calls=10,
            estimated_points=0,
            creation_order=1,
            created_at=now,
            updated_at=now,
        )
    )
    await db_session.flush()
    db_session.add(
        McpCall(
            id=str(uuid4()),
            logical_call_id=str(uuid4()),
            task_id=ids["task_id"],
            goal_id=None,
            batch_no=1,
            plan_step_id="g1_step_1",
            attempt=1,
            service_slug="insight-cube-mcp",
            internal_tool_name="datatap.insight.query.analysis.v1",
            arguments_digest="0" * 64,
            status="unknown",
            started_at=now - timedelta(hours=1),
            completed_at=unknown_completed_at or (now - timedelta(hours=1)),
            created_at=now,
            updated_at=now,
        )
    )
    if with_goals:
        db_session.add(
            TaskGoal(
                id=str(uuid4()),
                task_id=ids["task_id"],
                sequence=1,
                goal_type="brand_analysis",
                status="running",
                params_json={"brand": "海底捞"},
                started_at=now - timedelta(hours=1),
                created_at=now,
                updated_at=now,
            )
        )
        db_session.add(
            TaskGoal(
                id=str(uuid4()),
                task_id=ids["task_id"],
                sequence=2,
                goal_type="kol_selection",
                status="pending",
                params_json={"brand": "海底捞"},
                created_at=now,
                updated_at=now,
            )
        )
        db_session.add(
            TaskGoal(
                id=str(uuid4()),
                task_id=ids["task_id"],
                sequence=3,
                goal_type="campaign_analysis",
                status="completed",
                params_json={"brand": "海底捞", "campaign": "618"},
                started_at=now - timedelta(hours=2),
                completed_at=now - timedelta(hours=2),
                created_at=now,
                updated_at=now,
            )
        )
    await db_session.flush()
    return ids


@pytest.mark.asyncio
async def test_release_expired_unknown_fails_non_terminal_goals(db_session) -> None:
    ids = await _seed_task_with_goals(db_session)

    released = await TaskRepository(db_session).release_expired_unknown(ids["task_id"], 60)

    assert released is True
    task = await db_session.get(AnalysisTask, ids["task_id"])
    assert task is not None
    assert task.status == "failed"
    assert task.error_code == "mcp_unknown_outcome"

    goals = list(
        (
            await db_session.scalars(
                select(TaskGoal)
                .where(TaskGoal.task_id == ids["task_id"])
                .order_by(TaskGoal.sequence)
            )
        ).all()
    )
    # running 与 pending goal 都级联 failed，error_code 与任务一致，completed_at 落时间。
    assert goals[0].status == "failed"
    assert goals[0].error_code == "mcp_unknown_outcome"
    assert goals[0].completed_at is not None
    assert goals[1].status == "failed"
    assert goals[1].error_code == "mcp_unknown_outcome"
    assert goals[1].completed_at is not None
    # 已终态 goal 不动。
    assert goals[2].status == "completed"
    assert goals[2].error_code is None

    goal_failed_events = list(
        (
            await db_session.scalars(
                select(TaskEvent).where(
                    TaskEvent.task_id == ids["task_id"],
                    TaskEvent.event_type == "goal.failed",
                )
            )
        ).all()
    )
    assert len(goal_failed_events) == 2
    payloads = {event.payload_json["goal_id"]: event.payload_json for event in goal_failed_events}
    assert payloads[goals[0].id] == {
        "goal_id": goals[0].id,
        "goal_type": "brand_analysis",
        "status": "failed",
        "error_code": "mcp_unknown_outcome",
    }
    assert payloads[goals[1].id] == {
        "goal_id": goals[1].id,
        "goal_type": "kol_selection",
        "status": "failed",
        "error_code": "mcp_unknown_outcome",
    }


@pytest.mark.asyncio
async def test_release_expired_unknown_legacy_task_without_goals(db_session) -> None:
    ids = await _seed_task_with_goals(db_session, with_goals=False)

    released = await TaskRepository(db_session).release_expired_unknown(ids["task_id"], 60)

    assert released is True
    task = await db_session.get(AnalysisTask, ids["task_id"])
    assert task is not None
    assert task.status == "failed"
    assert task.error_code == "mcp_unknown_outcome"
    goal_events = await db_session.scalar(
        select(TaskEvent).where(
            TaskEvent.task_id == ids["task_id"],
            TaskEvent.event_type == "goal.failed",
        )
    )
    assert goal_events is None


@pytest.mark.asyncio
async def test_release_expired_unknown_skips_recent_unknown_calls(db_session) -> None:
    recent = datetime.now(UTC).replace(tzinfo=None)
    ids = await _seed_task_with_goals(db_session, unknown_completed_at=recent)

    released = await TaskRepository(db_session).release_expired_unknown(ids["task_id"], 3600)

    assert released is False
    goals = list(
        (
            await db_session.scalars(
                select(TaskGoal).where(TaskGoal.task_id == ids["task_id"])
            )
        ).all()
    )
    assert [goal.status for goal in goals] == ["running", "pending", "completed"]


# ---------------------------------------------------------------------------
# claim_lease 取消分支：级联 skipped 非终态 task_goals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_lease_cancel_skips_non_terminal_goals(db_session) -> None:
    ids = await _seed_task_with_goals(db_session, cancel_requested=True)

    claimed = await TaskRepository(db_session).claim_lease(ids["task_id"], "worker-1", 60)

    assert claimed is None
    task = await db_session.get(AnalysisTask, ids["task_id"])
    assert task is not None
    assert task.status == "cancelled"

    goals = list(
        (
            await db_session.scalars(
                select(TaskGoal)
                .where(TaskGoal.task_id == ids["task_id"])
                .order_by(TaskGoal.sequence)
            )
        ).all()
    )
    # running 与 pending goal 级联 skipped，completed_at 落时间；已终态 goal 不动。
    assert goals[0].status == "skipped"
    assert goals[0].completed_at is not None
    assert goals[0].error_code is None
    assert goals[1].status == "skipped"
    assert goals[1].completed_at is not None
    assert goals[2].status == "completed"

    goal_failed_events = list(
        (
            await db_session.scalars(
                select(TaskEvent).where(
                    TaskEvent.task_id == ids["task_id"],
                    TaskEvent.event_type == "goal.failed",
                )
            )
        ).all()
    )
    assert len(goal_failed_events) == 2
    payloads = {event.payload_json["goal_id"]: event.payload_json for event in goal_failed_events}
    # 与 executor 取消路径同构：status="skipped"，无 error_code 键。
    assert payloads[goals[0].id] == {
        "goal_id": goals[0].id,
        "goal_type": "brand_analysis",
        "status": "skipped",
    }
    assert payloads[goals[1].id] == {
        "goal_id": goals[1].id,
        "goal_type": "kol_selection",
        "status": "skipped",
    }


@pytest.mark.asyncio
async def test_claim_lease_cancel_legacy_task_without_goals(db_session) -> None:
    ids = await _seed_task_with_goals(
        db_session, with_goals=False, cancel_requested=True
    )

    claimed = await TaskRepository(db_session).claim_lease(ids["task_id"], "worker-1", 60)

    assert claimed is None
    task = await db_session.get(AnalysisTask, ids["task_id"])
    assert task is not None
    assert task.status == "cancelled"
    goal_events = await db_session.scalar(
        select(TaskEvent).where(
            TaskEvent.task_id == ids["task_id"],
            TaskEvent.event_type == "goal.failed",
        )
    )
    assert goal_events is None


@pytest.mark.asyncio
async def test_claim_lease_without_cancel_request_leaves_goals_untouched(db_session) -> None:
    ids = await _seed_task_with_goals(db_session, status="pending")

    claimed = await TaskRepository(db_session).claim_lease(ids["task_id"], "worker-1", 60)

    assert claimed is not None
    assert claimed.lease_owner == "worker-1"
    goals = list(
        (
            await db_session.scalars(
                select(TaskGoal).where(TaskGoal.task_id == ids["task_id"])
            )
        ).all()
    )
    assert [goal.status for goal in goals] == ["running", "pending", "completed"]
