"""create_task enforce 模式：GoalPlanner 接管单 Goal 规划（GOAL_PLANNER_ENFORCE_ENABLED）。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.goals.models import TaskGoal
from app.goals.planner import GoalPlannerService
from app.goals.schemas import (
    GoalParams,
    GoalPeriod,
    GoalPlannerOutput,
    GoalQuestion,
    GoalSpec,
)
from app.tasks.models import AnalysisTask
from app.workspace.models import Message, WorkspaceSession


def _enable_enforce(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "goal_planner_enforce_enabled", True)


def _clarify_output() -> GoalPlannerOutput:
    return GoalPlannerOutput(
        action="clarify",
        question=GoalQuestion(text="想看哪个品牌的分析？", options=["海底捞", "喜茶"]),
    )


def _spec(
    sequence: int,
    goal_type: str,
    *,
    brand: str | None = "海底捞",
    campaign: str | None = None,
    period: GoalPeriod | None = None,
    platforms: list[str] | None = None,
    requirement: str = "",
) -> GoalSpec:
    return GoalSpec(
        sequence=sequence,
        goal_type=goal_type,
        params=GoalParams(
            brand=brand,
            campaign=campaign,
            period=period,
            platforms=platforms or [],
            requirement=requirement,
        ),
        request_evidence="声量与互动数据",
    )


async def _create_session(client) -> str:
    created = await client.post("/api/v1/sessions", json={})
    assert created.status_code == 201
    return created.json()["id"]


async def _set_session_profile(db_session, session_id: str, *, brand: str, category: str) -> None:
    session = await db_session.get(WorkspaceSession, session_id)
    assert session is not None
    session.brand = brand
    session.category = category
    await db_session.flush()


@pytest.mark.asyncio
async def test_enforce_clarify_stores_message_without_task(
    auth_client_factory, db_session, monkeypatch
) -> None:
    _enable_enforce(monkeypatch)

    async def fake_plan(self, context):
        return _clarify_output()

    monkeypatch.setattr(GoalPlannerService, "plan_context", fake_plan)
    client = await auth_client_factory("13400000081")
    session_id = await _create_session(client)

    response = await client.post(
        f"/api/v1/sessions/{session_id}/tasks", json={"content": "帮我做个分析"}
    )

    assert response.status_code == 202
    body = response.json()
    assert body["outcome"] == "clarify"
    assert body["message"]["role"] == "assistant"
    assert body["message"]["content"] == "想看哪个品牌的分析？"
    assert body["message"]["metadata"]["clarify"] == {"options": ["海底捞", "喜茶"]}
    # 不落任务：analysis_tasks 为空，assistant 消息已持久化。
    task_count = await db_session.scalar(select(func.count()).select_from(AnalysisTask))
    assert task_count == 0
    persisted = await db_session.scalar(
        select(Message).where(
            Message.session_id == session_id, Message.role == "assistant"
        )
    )
    assert persisted is not None
    assert persisted.metadata_json["clarify"] == {"options": ["海底捞", "喜茶"]}


@pytest.mark.asyncio
async def test_enforce_execute_creates_typed_goal_with_params(
    auth_client_factory, db_session, monkeypatch
) -> None:
    _enable_enforce(monkeypatch)

    async def fake_plan(self, context):
        return GoalPlannerOutput(
            action="execute",
            goals=[
                _spec(
                    1,
                    "brand_analysis",
                    brand="喜茶",
                    period=GoalPeriod(start="2026-06-01", end="2026-06-30"),
                    platforms=["xiaohongshu"],
                    requirement="看品牌声量",
                )
            ],
        )

    monkeypatch.setattr(GoalPlannerService, "plan_context", fake_plan)
    client = await auth_client_factory("13400000082")
    session_id = await _create_session(client)
    await _set_session_profile(db_session, session_id, brand="海底捞", category="美食")

    response = await client.post(
        f"/api/v1/sessions/{session_id}/tasks", json={"content": "分析喜茶六月声量"}
    )

    assert response.status_code == 202
    body = response.json()
    assert body["outcome"] == "task"
    goal = await db_session.scalar(select(TaskGoal))
    assert goal is not None
    assert goal.goal_type == "brand_analysis"
    # planner params 合并会话快照：brand 被 planner 覆盖，category 来自会话。
    assert goal.params_json == {
        "brand": "喜茶",
        "category": "美食",
        "period": {"start": "2026-06-01", "end": "2026-06-30"},
        "platforms": ["xiaohongshu"],
        "requirement": "看品牌声量",
    }


@pytest.mark.asyncio
async def test_enforce_multi_goal_keeps_only_first(
    auth_client_factory, db_session, monkeypatch
) -> None:
    _enable_enforce(monkeypatch)

    async def fake_plan(self, context):
        return GoalPlannerOutput(
            action="execute",
            goals=[
                _spec(1, "brand_analysis", brand="海底捞"),
                _spec(2, "campaign_analysis", brand="海底捞", campaign="618"),
            ],
        )

    monkeypatch.setattr(GoalPlannerService, "plan_context", fake_plan)
    client = await auth_client_factory("13400000083")
    session_id = await _create_session(client)

    response = await client.post(
        f"/api/v1/sessions/{session_id}/tasks", json={"content": "分析品牌也看活动"}
    )

    assert response.status_code == 202
    assert response.json()["outcome"] == "task"
    goals = list((await db_session.scalars(select(TaskGoal))).all())
    # 复合编排属阶段四：>1 个 goal 只建 sequence=1 的。
    assert len(goals) == 1
    assert goals[0].goal_type == "brand_analysis"


@pytest.mark.asyncio
async def test_enforce_planner_failure_falls_back_to_kol_selection(
    auth_client_factory, db_session, monkeypatch
) -> None:
    _enable_enforce(monkeypatch)

    async def failing_plan(self, context):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(GoalPlannerService, "plan_context", failing_plan)
    client = await auth_client_factory("13400000084")
    session_id = await _create_session(client)

    response = await client.post(
        f"/api/v1/sessions/{session_id}/tasks", json={"content": "圈选美食达人"}
    )

    assert response.status_code == 202
    assert response.json()["outcome"] == "task"
    goal = await db_session.scalar(select(TaskGoal))
    assert goal is not None
    assert goal.goal_type == "kol_selection"


@pytest.mark.asyncio
async def test_enforce_disabled_keeps_legacy_path(
    auth_client_factory, db_session, monkeypatch
) -> None:
    called = False

    async def forbidden_plan(self, context):
        nonlocal called
        called = True
        raise AssertionError("planner must not run when enforce is off")

    monkeypatch.setattr(GoalPlannerService, "plan_context", forbidden_plan)
    client = await auth_client_factory("13400000085")
    session_id = await _create_session(client)

    response = await client.post(
        f"/api/v1/sessions/{session_id}/tasks", json={"content": "圈选美食达人"}
    )

    assert response.status_code == 202
    assert response.json()["outcome"] == "task"
    assert called is False
    goal = await db_session.scalar(select(TaskGoal))
    assert goal is not None
    assert goal.goal_type == "kol_selection"


@pytest.mark.asyncio
async def test_enforce_idempotent_hit_skips_planner(
    auth_client_factory, db_session, monkeypatch
) -> None:
    _enable_enforce(monkeypatch)
    calls = 0

    async def counting_plan(self, context):
        nonlocal calls
        calls += 1
        return GoalPlannerOutput(action="execute", goals=[_spec(1, "kol_selection")])

    monkeypatch.setattr(GoalPlannerService, "plan_context", counting_plan)
    client = await auth_client_factory("13400000086")
    session_id = await _create_session(client)
    headers = {"Idempotency-Key": "enforce-key-1"}

    first = await client.post(
        f"/api/v1/sessions/{session_id}/tasks",
        json={"content": "圈选美食达人"},
        headers=headers,
    )
    second = await client.post(
        f"/api/v1/sessions/{session_id}/tasks",
        json={"content": "圈选美食达人"},
        headers=headers,
    )

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["outcome"] == "task"
    assert second.json()["outcome"] == "task"
    assert second.json()["task"]["id"] == first.json()["task"]["id"]
    # 幂等命中不重复调 planner。
    assert calls == 1
    task_count = await db_session.scalar(select(func.count()).select_from(AnalysisTask))
    assert task_count == 1


@pytest.mark.asyncio
async def test_task_service_create_accepts_goal_overrides(db_session, user_factory) -> None:
    """TaskService.create 按入参建 goal；缺省保持 kol_selection + 会话快照。"""
    from app.tasks.schemas import TaskCreate
    from app.tasks.service import TaskService

    user = await user_factory()
    now = datetime.now(UTC).replace(tzinfo=None)
    session = WorkspaceSession(
        id=str(uuid4()),
        user_id=user.id,
        title="goal 覆盖测试",
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
    db_session.add(session)
    await db_session.flush()
    service = TaskService(db_session)

    default_task = await service.create(user.id, session.id, TaskCreate(content="第一条"))
    default_task.status = "completed"
    await db_session.flush()
    custom_task = await service.create(
        user.id,
        session.id,
        TaskCreate(content="第二条"),
        goal_type="campaign_analysis",
        goal_params={"brand": "喜茶", "campaign": "618大促"},
    )

    goals = {goal.task_id: goal for goal in (await db_session.scalars(select(TaskGoal))).all()}
    assert goals[default_task.id].goal_type == "kol_selection"
    assert goals[default_task.id].params_json == {"brand": "海底捞", "category": "美食"}
    assert goals[custom_task.id].goal_type == "campaign_analysis"
    assert goals[custom_task.id].params_json == {
        "brand": "喜茶",
        "category": "美食",
        "campaign": "618大促",
    }
