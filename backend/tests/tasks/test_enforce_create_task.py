"""create_task enforce 模式：GoalPlanner 接管单 Goal 规划（GOAL_PLANNER_ENFORCE_ENABLED）。"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError
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
from app.mcp_gateway.models import McpToolCatalog
from app.model.tencent_plan import TencentPlanAdapter
from app.tasks.models import AnalysisTask
from app.tasks.schemas import TaskCreate
from app.tasks.service import TaskService
from app.thinking.service import SessionThinkingService
from app.workspace.models import Message, WorkspaceSession


class _GoalPlannerCompletions:
    async def create(self, **kwargs):
        assert kwargs["stream"] is True
        content = (
            '{"action":"execute","question":null,"goals":[{"sequence":1,'
            '"goal_type":"brand_analysis","depends_on_sequence":null,'
            '"params":{"brand":"喜茶","campaign":null,"period":null,'
            '"platforms":[],"requirement":"分析六月声量"},'
            '"request_evidence":"分析喜茶六月声量"}],'
            '"active_brand":"喜茶","brand_source":"explicit"}'
        )
        chunks = [
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content=f"<think>先识别分析目标。</think>{content}",
                            reasoning_content=None,
                        ),
                        finish_reason=None,
                    )
                ],
                usage=None,
                _request_id="req-goal-planner",
            ),
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content=None, reasoning_content=None),
                        finish_reason="stop",
                    )
                ],
                usage=None,
                _request_id="req-goal-planner",
            ),
        ]

        async def stream():
            for chunk in chunks:
                yield chunk

        return stream()


class _FailingThinkingSink:
    async def started(self, *, attempt: int) -> None:
        raise RuntimeError("sink down")

    async def delta(self, text: str, *, attempt: int) -> None:
        raise RuntimeError("sink down")

    async def completed(self, *, attempt: int, duration_ms: int) -> None:
        raise RuntimeError("sink down")

    async def failed(self, *, attempt: int, error_code: str) -> None:
        raise RuntimeError("sink down")


async def _ignore_prompt_log(_entry) -> None:
    return None


def _share_session_factory(monkeypatch, db_session) -> None:
    """commit 后的思考持久化走 SessionFactory 独立事务；测试 fixture 是共享连接 +
    savepoint，真实 SessionFactory 的新连接看不到未提交数据（会撞 InnoDB 锁等待），
    需替换为共享会话。先例：tests/brainstorm/test_brainstorm.py。"""

    class _SessionCM:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *_args):
            return None

    class _SessionFactory:
        @staticmethod
        def begin():
            return _SessionCM()

    monkeypatch.setattr("app.tasks.router.SessionFactory", _SessionFactory)


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


def test_task_create_accepts_turn_id_and_rejects_invalid_uuid() -> None:
    turn_id = "8a9fda07-77c5-44ea-967e-a17e795266ef"

    assert str(TaskCreate(content="分析品牌", turn_id=turn_id).turn_id) == turn_id
    with pytest.raises(ValidationError):
        TaskCreate(content="分析品牌", turn_id="not-a-uuid")


@pytest.mark.asyncio
async def test_enforce_clarify_stores_message_without_task(
    auth_client_factory, db_session, monkeypatch
) -> None:
    _enable_enforce(monkeypatch)

    async def fake_plan(self, context, **kwargs):
        assert kwargs["thinking_sink"] is not None
        return _clarify_output()

    monkeypatch.setattr(GoalPlannerService, "plan_context", fake_plan)
    _share_session_factory(monkeypatch, db_session)
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
    assert body["message"]["metadata"]["turn_id"]
    # 不落任务：analysis_tasks 为空，assistant 消息已持久化。
    task_count = await db_session.scalar(select(func.count()).select_from(AnalysisTask))
    assert task_count == 0
    persisted = list(
        (
            await db_session.scalars(
                select(Message)
                .where(Message.session_id == session_id)
                .order_by(Message.sequence)
            )
        ).all()
    )
    assert [message.role for message in persisted] == ["user", "assistant"]
    assert persisted[0].metadata_json["turn_id"] == persisted[1].metadata_json["turn_id"]
    assert persisted[1].metadata_json["clarify"] == {"options": ["海底捞", "喜茶"]}


def _catalog_row(
    *,
    internal_name: str = "kol_search",
    is_enabled: bool = True,
    review_status: str = "approved",
) -> McpToolCatalog:
    now = datetime.now(UTC).replace(tzinfo=None)
    return McpToolCatalog(
        id=str(uuid4()),
        service_slug="social-grow-mcp",
        internal_tool_name=internal_name,
        reviewed_description="按平台与标签检索达人",
        input_schema_json={
            "type": "object",
            "required": ["platform"],
            "properties": {"platform": {"type": "string"}},
        },
        output_validator_version="kol_search_v1",
        discovery_digest="d" * 64,
        review_status=review_status,
        is_enabled=is_enabled,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_enforce_injects_available_tools_into_planner_context(
    auth_client_factory, db_session, monkeypatch
) -> None:
    """enforce 规划上下文注入已审核工具紧凑投影；quarantined/disabled 不注入。"""
    _enable_enforce(monkeypatch)
    db_session.add(_catalog_row())
    db_session.add(_catalog_row(internal_name="kol_detail_quarantined", review_status="quarantined"))
    db_session.add(_catalog_row(internal_name="kol_detail_disabled", is_enabled=False))
    await db_session.flush()
    captured: dict[str, object] = {}

    async def fake_plan(self, context, **kwargs):
        captured["available_tools"] = context.available_tools
        return _clarify_output()

    monkeypatch.setattr(GoalPlannerService, "plan_context", fake_plan)
    _share_session_factory(monkeypatch, db_session)
    client = await auth_client_factory("13400000091")
    session_id = await _create_session(client)

    response = await client.post(
        f"/api/v1/sessions/{session_id}/tasks", json={"content": "帮我圈选达人"}
    )

    assert response.status_code == 202
    assert captured["available_tools"] == (
        {
            "internal_name": "kol_search",
            "description": "按平台与标签检索达人",
            "required_params": ["platform"],
        },
    )


@pytest.mark.asyncio
async def test_enforce_clarify_then_answer_executes_with_history(
    auth_client_factory, db_session, monkeypatch
) -> None:
    """首轮 clarify 不落任务；用户回答后重新规划能看到澄清问答并 execute 建任务。"""
    _enable_enforce(monkeypatch)
    contexts = []

    async def fake_plan(self, context, **kwargs):
        contexts.append(context)
        if len(contexts) == 1:
            return _clarify_output()
        return GoalPlannerOutput(
            action="execute",
            goals=[_spec(1, "brand_analysis", brand="喜茶", requirement="六月声量")],
        )

    monkeypatch.setattr(GoalPlannerService, "plan_context", fake_plan)
    _share_session_factory(monkeypatch, db_session)
    client = await auth_client_factory("13400000092")
    session_id = await _create_session(client)
    await _set_session_profile(db_session, session_id, brand="喜茶", category="茶饮")

    first = await client.post(
        f"/api/v1/sessions/{session_id}/tasks", json={"content": "分析喜茶声量"}
    )
    assert first.status_code == 202
    assert first.json()["outcome"] == "clarify"

    second = await client.post(
        f"/api/v1/sessions/{session_id}/tasks", json={"content": "看六月的数据"}
    )
    assert second.status_code == 202
    assert second.json()["outcome"] == "task"

    # 第二次规划的 recent_messages 含首轮澄清问答与本次回答。
    history = [message.content for message in contexts[1].recent_messages]
    assert "分析喜茶声量" in history
    assert "想看哪个品牌的分析？" in history
    assert contexts[1].recent_messages[-1].content == "看六月的数据"
    goal = await db_session.scalar(select(TaskGoal))
    assert goal is not None
    assert goal.goal_type == "brand_analysis"


@pytest.mark.asyncio
async def test_enforce_execute_creates_typed_goal_with_params(
    auth_client_factory, db_session, monkeypatch
) -> None:
    _enable_enforce(monkeypatch)

    async def fake_plan(self, context, **_kwargs):
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
    _share_session_factory(monkeypatch, db_session)
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
async def test_enforce_creates_planned_goal_when_every_thinking_sink_method_fails(
    auth_client_factory, db_session, monkeypatch
) -> None:
    _enable_enforce(monkeypatch)
    monkeypatch.setattr(
        SessionThinkingService,
        "create_sink",
        lambda _self, _spec: _FailingThinkingSink(),
    )
    adapter = TencentPlanAdapter(
        client=_GoalPlannerCompletions(),
        log_writer=_ignore_prompt_log,
        stream_support_cache={},
    )
    monkeypatch.setattr("app.tasks.router.get_model_adapter", lambda: adapter)
    _share_session_factory(monkeypatch, db_session)
    client = await auth_client_factory("13400000088")
    session_id = await _create_session(client)
    await _set_session_profile(db_session, session_id, brand="海底捞", category="美食")

    response = await client.post(
        f"/api/v1/sessions/{session_id}/tasks",
        json={"content": "分析喜茶六月声量"},
    )

    assert response.status_code == 202
    assert response.json()["outcome"] == "task"
    goal = await db_session.scalar(select(TaskGoal))
    assert goal is not None
    assert goal.goal_type == "brand_analysis"
    assert goal.params_json["brand"] == "喜茶"


@pytest.mark.asyncio
async def test_enforce_multi_goal_persists_all_with_dependency(
    auth_client_factory, db_session, monkeypatch
) -> None:
    _enable_enforce(monkeypatch)

    async def fake_plan(self, context, **_kwargs):
        return GoalPlannerOutput(
            action="execute",
            goals=[
                _spec(1, "campaign_analysis", brand="海底捞", campaign="618大促"),
                _spec(2, "kol_selection", brand="海底捞"),
            ],
        )

    monkeypatch.setattr(GoalPlannerService, "plan_context", fake_plan)
    # planner 输出依赖：kol_selection 依赖 sequence=1（GoalSpec.depends_on_sequence）。
    original_spec = _spec

    async def fake_plan_with_dependency(self, context, **_kwargs):
        first = original_spec(1, "campaign_analysis", brand="海底捞", campaign="618大促")
        second = original_spec(2, "kol_selection", brand="海底捞")
        return GoalPlannerOutput(
            action="execute",
            goals=[first, second.model_copy(update={"depends_on_sequence": 1})],
        )

    monkeypatch.setattr(GoalPlannerService, "plan_context", fake_plan_with_dependency)
    _share_session_factory(monkeypatch, db_session)
    client = await auth_client_factory("13400000083")
    session_id = await _create_session(client)

    response = await client.post(
        f"/api/v1/sessions/{session_id}/tasks", json={"content": "复盘海底捞 618 并圈选达人"}
    )

    assert response.status_code == 202
    assert response.json()["outcome"] == "task"
    goals = list(
        (
            await db_session.scalars(select(TaskGoal).order_by(TaskGoal.sequence))
        ).all()
    )
    # 阶段四顺序编排：planner 输出的 1-3 个 goal 全部落库，依赖解析为 id。
    assert len(goals) == 2
    assert goals[0].goal_type == "campaign_analysis"
    assert goals[0].sequence == 1
    assert goals[0].depends_on_goal_id is None
    assert goals[1].goal_type == "kol_selection"
    assert goals[1].sequence == 2
    assert goals[1].depends_on_goal_id == goals[0].id


@pytest.mark.asyncio
async def test_enforce_planner_failure_falls_back_to_kol_selection(
    auth_client_factory, db_session, monkeypatch
) -> None:
    _enable_enforce(monkeypatch)

    async def failing_plan(self, context, **_kwargs):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(GoalPlannerService, "plan_context", failing_plan)
    _share_session_factory(monkeypatch, db_session)
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

    async def forbidden_plan(self, context, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("planner must not run when enforce is off")

    monkeypatch.setattr(GoalPlannerService, "plan_context", forbidden_plan)
    _share_session_factory(monkeypatch, db_session)
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

    async def counting_plan(self, context, **_kwargs):
        nonlocal calls
        calls += 1
        return GoalPlannerOutput(action="execute", goals=[_spec(1, "kol_selection")])

    monkeypatch.setattr(GoalPlannerService, "plan_context", counting_plan)
    _share_session_factory(monkeypatch, db_session)
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


@pytest.mark.asyncio
async def test_task_service_create_persists_turn_id_and_retry_reuses_it(
    db_session, user_factory
) -> None:
    user = await user_factory()
    now = datetime.now(UTC).replace(tzinfo=None)
    session = WorkspaceSession(
        id=str(uuid4()),
        user_id=user.id,
        title="turn 持久化测试",
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
    turn_id = "8a9fda07-77c5-44ea-967e-a17e795266ef"

    source = await TaskService(db_session).create(
        user.id,
        session.id,
        TaskCreate(content="分析品牌", turn_id=turn_id),
    )
    source.status = "completed"
    await db_session.flush()
    retry = await TaskService(db_session).retry(user.id, source.id)
    message = await db_session.get(Message, source.trigger_message_id)

    assert message is not None
    assert retry.trigger_message_id == source.trigger_message_id
    assert message.metadata_json["turn_id"] == turn_id


@pytest.mark.asyncio
async def test_task_service_create_persists_goal_specs_with_dependency(
    db_session, user_factory
) -> None:
    """goal_specs 列表：按 sequence 建多行 TaskGoal，depends_on_sequence 解析为 id。"""
    user = await user_factory()
    now = datetime.now(UTC).replace(tzinfo=None)
    session = WorkspaceSession(
        id=str(uuid4()),
        user_id=user.id,
        title="多 goal 落库测试",
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

    task = await TaskService(db_session).create(
        user.id,
        session.id,
        TaskCreate(content="复盘海底捞 618 并分析品牌再圈选达人"),
        goal_specs=[
            {
                "goal_type": "campaign_analysis",
                "sequence": 1,
                "depends_on_sequence": None,
                "params": {"brand": "海底捞", "campaign": "618大促"},
            },
            {
                "goal_type": "kol_selection",
                "sequence": 2,
                "depends_on_sequence": 1,
                "params": {"brand": "海底捞"},
            },
            {
                "goal_type": "brand_analysis",
                "sequence": 3,
                "depends_on_sequence": None,
                "params": {"brand": "海底捞"},
            },
        ],
    )

    goals = list(
        (
            await db_session.scalars(
                select(TaskGoal)
                .where(TaskGoal.task_id == task.id)
                .order_by(TaskGoal.sequence)
            )
        ).all()
    )
    assert [goal.goal_type for goal in goals] == [
        "campaign_analysis",
        "kol_selection",
        "brand_analysis",
    ]
    assert [goal.sequence for goal in goals] == [1, 2, 3]
    assert goals[0].depends_on_goal_id is None
    assert goals[1].depends_on_goal_id == goals[0].id
    assert goals[2].depends_on_goal_id is None
    # params 各自合并会话快照（category 来自会话）。
    assert goals[0].params_json == {"brand": "海底捞", "category": "美食", "campaign": "618大促"}
    assert goals[1].params_json == {"brand": "海底捞", "category": "美食"}
    assert all(goal.status == "pending" for goal in goals)


def _respond_output(respond_type: str) -> GoalPlannerOutput:
    return GoalPlannerOutput(action="respond", respond_type=respond_type)


@pytest.mark.asyncio
async def test_enforce_respond_usage_help_returns_static_message(
    auth_client_factory, db_session, monkeypatch
) -> None:
    _enable_enforce(monkeypatch)

    async def fake_plan(self, context, **kwargs):
        return _respond_output("usage_help")

    monkeypatch.setattr(GoalPlannerService, "plan_context", fake_plan)
    _share_session_factory(monkeypatch, db_session)
    client = await auth_client_factory("13400000095")
    session_id = await _create_session(client)

    response = await client.post(
        f"/api/v1/sessions/{session_id}/tasks", json={"content": "这个产品怎么用？"}
    )

    assert response.status_code == 202
    body = response.json()
    assert body["outcome"] == "respond"
    assert body["respond_type"] == "usage_help"
    assert "使用方法" in body["message"]["content"]
    assert body["message"]["metadata"]["respond"] == {"type": "usage_help"}
    task_count = await db_session.scalar(select(func.count()).select_from(AnalysisTask))
    assert task_count == 0


@pytest.mark.asyncio
async def test_enforce_respond_out_of_scope_rejects_politely(
    auth_client_factory, db_session, monkeypatch
) -> None:
    _enable_enforce(monkeypatch)

    async def fake_plan(self, context, **kwargs):
        return _respond_output("out_of_scope")

    monkeypatch.setattr(GoalPlannerService, "plan_context", fake_plan)
    _share_session_factory(monkeypatch, db_session)
    client = await auth_client_factory("13400000096")
    session_id = await _create_session(client)

    response = await client.post(
        f"/api/v1/sessions/{session_id}/tasks", json={"content": "帮我写一段 Python 代码"}
    )

    assert response.status_code == 202
    body = response.json()
    assert body["respond_type"] == "out_of_scope"
    assert "营销分析助手" in body["message"]["content"]


@pytest.mark.asyncio
async def test_enforce_respond_context_qa_uses_model_answer(
    auth_client_factory, db_session, monkeypatch
) -> None:
    _enable_enforce(monkeypatch)

    async def fake_plan(self, context, **kwargs):
        return _respond_output("context_qa")

    monkeypatch.setattr(GoalPlannerService, "plan_context", fake_plan)

    async def fake_answer(db, model, *, user_id, session_id, question):
        return "上次失败是因为未采集到有效数据。"

    monkeypatch.setattr("app.tasks.router.answer_context_qa", fake_answer)
    _share_session_factory(monkeypatch, db_session)
    client = await auth_client_factory("13400000097")
    session_id = await _create_session(client)

    response = await client.post(
        f"/api/v1/sessions/{session_id}/tasks", json={"content": "为什么上次失败了？"}
    )

    assert response.status_code == 202
    body = response.json()
    assert body["respond_type"] == "context_qa"
    assert body["message"]["content"] == "上次失败是因为未采集到有效数据。"
    task_count = await db_session.scalar(select(func.count()).select_from(AnalysisTask))
    assert task_count == 0
    persisted = list(
        (
            await db_session.scalars(
                select(Message)
                .where(Message.session_id == session_id)
                .order_by(Message.sequence)
            )
        ).all()
    )
    assert [message.role for message in persisted] == ["user", "assistant"]
    assert persisted[1].metadata_json["respond"] == {"type": "context_qa"}


@pytest.mark.asyncio
async def test_enforce_execute_task_exists_when_thinking_persist_fails(
    auth_client_factory, db_session, monkeypatch
) -> None:
    """思考持久化（commit 后独立事务）失败不得影响任务落库与 202 响应。"""
    _enable_enforce(monkeypatch)

    async def fake_plan(self, context, **_kwargs):
        return GoalPlannerOutput(action="execute", goals=[_spec(1, "kol_selection")])

    async def failing_persist(*_args, **_kwargs):
        raise RuntimeError("thinking persist down")

    monkeypatch.setattr(GoalPlannerService, "plan_context", fake_plan)
    monkeypatch.setattr("app.tasks.router.persist_turn_thinking", failing_persist)
    client = await auth_client_factory("13400000098")
    session_id = await _create_session(client)

    response = await client.post(
        f"/api/v1/sessions/{session_id}/tasks", json={"content": "圈选美食达人"}
    )

    assert response.status_code == 202
    body = response.json()
    assert body["outcome"] == "task"
    # 响应的 task_id 真实存在于 analysis_tasks（非幽灵任务）。
    task = await db_session.get(AnalysisTask, body["task"]["id"])
    assert task is not None
    assert task.session_id == session_id
    goal = await db_session.scalar(select(TaskGoal))
    assert goal is not None
    assert goal.task_id == task.id


@pytest.mark.asyncio
async def test_enforce_clarify_survives_thinking_persist_failure(
    auth_client_factory, db_session, monkeypatch
) -> None:
    """思考持久化失败不得影响 clarify 消息落库（GET 恢复可见）。"""
    _enable_enforce(monkeypatch)

    async def fake_plan(self, context, **_kwargs):
        return _clarify_output()

    async def failing_persist(*_args, **_kwargs):
        raise RuntimeError("thinking persist down")

    monkeypatch.setattr(GoalPlannerService, "plan_context", fake_plan)
    monkeypatch.setattr("app.tasks.router.persist_turn_thinking", failing_persist)
    client = await auth_client_factory("13400000099")
    session_id = await _create_session(client)

    response = await client.post(
        f"/api/v1/sessions/{session_id}/tasks", json={"content": "帮我做个分析"}
    )

    assert response.status_code == 202
    body = response.json()
    assert body["outcome"] == "clarify"
    # clarify 消息真实落库：GET 恢复可见。
    recovered = await client.get(f"/api/v1/sessions/{session_id}")
    assert recovered.status_code == 200
    messages = recovered.json()["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[1]["content"] == "想看哪个品牌的分析？"
    assert messages[1]["metadata"]["clarify"] == {"options": ["海底捞", "喜茶"]}
    assert messages[0]["metadata"]["turn_id"] == messages[1]["metadata"]["turn_id"]
