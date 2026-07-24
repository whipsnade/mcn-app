from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select

from app.artifacts.models import TaskArtifact
from app.db.session import SessionFactory
from app.goals.models import TaskGoal
from app.identity.models import User
from app.mcp_gateway.contracts import DataTapService
from app.orchestration.loop import AgentDecision, AgentLoopContext
from app.orchestration.schemas import PlannerTool
from app.reporting.analysis_reports import AnalysisReportService
from app.reporting.blocks import MetricGridBlock, MetricItem, ReportDocument
from app.reporting.models import AnalysisReport
from app.selection.models import KolSelectionItem, KolSelectionSet, SessionKolSelection
from app.selection.service import KolSelectionService
from app.tasks.dependencies import _TaskArtifacts
from app.tasks.executor import TaskExecutor
from app.tasks.models import AnalysisTask, TaskEvent
from app.tasks.schemas import TaskCreate
from app.tasks.service import TaskService
from app.workspace.models import Message, WorkspaceSession


# ---------------------------------------------------------------------------
# Part A：TaskService.create 同事务建 goal
# ---------------------------------------------------------------------------


async def _create_session(
    db_session, user_factory, *, brand="海底捞", category="美食"
) -> tuple[str, str]:
    user = await user_factory()
    now = datetime.now(UTC).replace(tzinfo=None)
    session = WorkspaceSession(
        id=str(uuid4()),
        user_id=user.id,
        title="goal 测试会话",
        brand=brand,
        campaign_name=None,
        status="active",
        platforms=["xiaohongshu"],
        category=category,
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
    return user.id, session.id


@pytest.mark.asyncio
async def test_create_task_creates_kol_selection_goal(db_session, user_factory) -> None:
    user_id, session_id = await _create_session(db_session, user_factory)
    service = TaskService(db_session)

    task = await service.create(user_id, session_id, TaskCreate(content="圈选美食达人"))

    goal = await db_session.scalar(select(TaskGoal).where(TaskGoal.task_id == task.id))
    assert goal is not None
    assert goal.goal_type == "kol_selection"
    assert goal.sequence == 1
    assert goal.status == "pending"
    assert goal.params_json == {"brand": "海底捞", "category": "美食"}


@pytest.mark.asyncio
async def test_create_task_goal_params_omit_none_fields(db_session, user_factory) -> None:
    user_id, session_id = await _create_session(db_session, user_factory, category=None)
    service = TaskService(db_session)

    task = await service.create(user_id, session_id, TaskCreate(content="圈选美食达人"))

    goal = await db_session.scalar(select(TaskGoal).where(TaskGoal.task_id == task.id))
    assert goal is not None
    assert goal.params_json == {"brand": "海底捞"}


@pytest.mark.asyncio
async def test_retry_task_creates_own_goal(db_session, user_factory) -> None:
    user_id, session_id = await _create_session(db_session, user_factory)
    service = TaskService(db_session)
    task = await service.create(user_id, session_id, TaskCreate(content="圈选美食达人"))
    task.status = "completed"
    await db_session.flush()

    retry = await service.retry(user_id, task.id)

    assert retry.id != task.id
    goals = list((await db_session.scalars(select(TaskGoal))).all())
    assert len(goals) == 2
    goal_by_task = {goal.task_id: goal for goal in goals}
    assert goal_by_task[task.id].id != goal_by_task[retry.id].id
    assert goal_by_task[retry.id].params_json == {"brand": "海底捞", "category": "美食"}


@pytest.mark.asyncio
async def test_create_idempotent_replay_does_not_duplicate_goal(
    db_session, user_factory
) -> None:
    user_id, session_id = await _create_session(db_session, user_factory)
    service = TaskService(db_session)

    first, reused_first = await service.create_idempotent(
        user_id, session_id, TaskCreate(content="圈选美食达人"), "key-1"
    )
    second, reused_second = await service.create_idempotent(
        user_id, session_id, TaskCreate(content="圈选美食达人"), "key-1"
    )

    assert reused_first is False
    assert reused_second is True
    assert second.id == first.id
    total = await db_session.scalar(select(func.count()).select_from(TaskGoal))
    assert total == 1


# ---------------------------------------------------------------------------
# Part B：executor goal 生命周期接线（纯 fake 单测）
# ---------------------------------------------------------------------------

_TOOL_NAME = "datatap.insight.social.statistic.overview.v1"
_TOOL_SCHEMA = {
    "type": "object",
    "properties": {"keyword": {"type": "string"}},
    "required": ["keyword"],
    "additionalProperties": False,
}


def _tool() -> PlannerTool:
    return PlannerTool(
        catalog_id="cat-1",
        internal_name=_TOOL_NAME,
        service=DataTapService.INSIGHT_CUBE,
        description="声量概览",
        input_schema=_TOOL_SCHEMA,
        output_schema={},
    )


def _call() -> AgentDecision:
    return AgentDecision(
        action="call_tool",
        internal_tool_name=_TOOL_NAME,
        arguments={"keyword": "美妆"},
        evidence_goal="声量概览",
    )


def _finish(conclusion: str = "") -> AgentDecision:
    return AgentDecision(action="finish", conclusion=conclusion)


def _settled() -> SimpleNamespace:
    return SimpleNamespace(
        status="settled",
        internal_tool_name=_TOOL_NAME,
        plan_step_id="step_1",
        evidence_json={"structured_content": {"total_volume": 12345}},
        error_type=None,
    )


def _goal() -> SimpleNamespace:
    return SimpleNamespace(
        id="goal-1",
        goal_type="kol_selection",
        sequence=1,
        params_json={"brand": "海底捞", "category": "美食"},
    )


class _FakeStore:
    """含 goal 接缝的存储 fake；goal=None 时等价 legacy 任务。"""

    def __init__(self, task: SimpleNamespace, goal: SimpleNamespace | None = None) -> None:
        self.task = task
        self.goal = goal
        self.events: list[tuple[str, dict]] = []
        self.terminal: str | None = None
        self.running_goals: list[str] = []

    async def claim_lease(self, task_id, worker_id, lease_seconds):
        return self.task

    async def save_plan(self, task_id, worker_id, plan_json):
        self.task.plan_json = plan_json
        return True

    async def save_trajectory(self, task_id, worker_id, trajectory_json):
        self.task.plan_json = trajectory_json
        return True

    async def cancel_requested(self, task_id):
        return False

    async def renew_lease(self, task_id, worker_id, lease_seconds):
        return True

    async def mark_completed(self, task_id, worker_id):
        self.terminal = "completed"
        return True

    async def mark_completed_with_warnings(self, task_id, worker_id, code, message=None):
        self.terminal = f"completed_with_warnings:{code}"
        return True

    async def mark_cancelled(self, task_id, worker_id):
        self.terminal = "cancelled"
        return True

    async def mark_interrupted(self, task_id, worker_id):
        self.terminal = "interrupted"
        return True

    async def mark_failed(self, task_id, worker_id, code, message=None):
        self.terminal = f"failed:{code}"
        return True

    async def mark_insufficient_balance(self, task_id, worker_id):
        self.terminal = "insufficient_balance"
        return True

    async def append_event(self, task_id, user_id, event_type, payload):
        self.events.append((str(event_type), payload))

    async def release_lease(self, task_id, worker_id):
        return None

    async def load_task_goal(self, task_id):
        return self.goal

    async def mark_goal_running(self, goal_id):
        self.running_goals.append(goal_id)
        return True


class _LegacyStore(_FakeStore):
    """无 goal 接缝的旧存储 fake（模拟未升级路径），不提供 load_task_goal。"""

    async def load_task_goal(self, task_id):  # pragma: no cover - 被 __getattr__ 屏蔽
        raise AssertionError("legacy store must not expose load_task_goal")

    def __getattr__(self, name):
        if name in {"load_task_goal", "mark_goal_running"}:
            raise AttributeError(name)
        raise AttributeError(name)


class _FakeContextBuilder:
    async def build_agent_context(self, user_id, session_id):
        return AgentLoopContext(
            recent_messages=(),
            tools=(_tool(),),
            allowed_channels=("xiaohongshu",),
        )


class _ScriptedDecider:
    def __init__(self, decisions: list[AgentDecision]) -> None:
        self._decisions = list(decisions)

    async def agent_decide(self, context):
        return self._decisions.pop(0)


class _FakeGateway:
    def __init__(self, rows: list) -> None:
        self._rows = list(rows)
        self.commands: list = []

    async def execute_batch(self, commands):
        self.commands.extend(commands)
        item = self._rows.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeArtifacts:
    def __init__(self, store: _FakeStore | None = None) -> None:
        self.calls: list[str] = []
        self.finalized: list[tuple[str, str | None, str | None]] = []
        self._store = store

    async def write_conclusion_message(self, task_id, conclusion):
        self.calls.append("conclusion")

    async def auto_kol_analysis(self, task_id):
        self.calls.append("auto_analysis")

    async def finalize_goal(self, task_id, *, terminal_status, error_code=None):
        # 记录调用时的任务终态，断言 goal 收尾先于任务终态。
        self.finalized.append(
            (terminal_status, error_code, self._store.terminal if self._store else None)
        )


class _FakeSelection:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def ingest(self, **kwargs):
        self.calls.append(kwargs)


def _task() -> SimpleNamespace:
    return SimpleNamespace(
        id="task-1",
        user_id="user-1",
        session_id="session-1",
        kind="agent",
        plan_json=None,
        retry_of_task_id=None,
    )


def _executor(store, decider, gateway, artifacts, selection=None) -> TaskExecutor:
    return TaskExecutor(
        repository=store,
        context_builder=_FakeContextBuilder(),
        planner=decider,
        gateway=gateway,
        artifacts=artifacts,
        selection=selection,
        worker_id="worker-1",
        lease_seconds=60,
        heartbeat_seconds=0.05,
    )


@pytest.mark.asyncio
async def test_goal_flow_completed_wires_goal_id_and_events() -> None:
    store = _FakeStore(_task(), goal=_goal())
    gateway = _FakeGateway([(_settled(),)])
    artifacts = _FakeArtifacts(store)
    selection = _FakeSelection()
    executor = _executor(store, _ScriptedDecider([_call(), _finish("done")]), gateway,
                         artifacts, selection)

    await executor.run("task-1")

    assert store.running_goals == ["goal-1"]
    event_types = [event_type for event_type, _ in store.events]
    assert event_types[0] == "goal.started"
    goal_started = store.events[0][1]
    assert goal_started == {"goal_id": "goal-1", "goal_type": "kol_selection", "sequence": 1}
    tool_started = next(payload for t, payload in store.events if t == "tool.started")
    assert tool_started["goal_id"] == "goal-1"
    tool_succeeded = next(payload for t, payload in store.events if t == "tool.succeeded")
    assert tool_succeeded["goal_id"] == "goal-1"
    assert gateway.commands[0].goal_id == "goal-1"
    assert len(selection.calls) == 1
    ingest_kwargs = selection.calls[0]
    assert ingest_kwargs["goal_id"] == "goal-1"
    assert ingest_kwargs["set_title"] == "海底捞圈选名单"
    assert ingest_kwargs["set_scope"] == {"brand": "海底捞", "category": "美食"}
    # goal 收尾在任务终态之前，终态为 completed。
    assert artifacts.finalized == [("completed", None, None)]
    assert store.terminal == "completed"


@pytest.mark.asyncio
async def test_goal_flow_insufficient_balance_marks_goal() -> None:
    from app.billing.service import InsufficientPointsError

    store = _FakeStore(_task(), goal=_goal())
    gateway = _FakeGateway([InsufficientPointsError("余额不足")])
    artifacts = _FakeArtifacts(store)
    executor = _executor(store, _ScriptedDecider([_call()]), gateway, artifacts)

    await executor.run("task-1")

    assert artifacts.finalized == [("insufficient_balance", None, None)]
    assert store.terminal == "insufficient_balance"


@pytest.mark.asyncio
async def test_goal_flow_no_evidence_fails_goal_with_error_code() -> None:
    store = _FakeStore(_task(), goal=_goal())
    gateway = _FakeGateway([])
    artifacts = _FakeArtifacts(store)
    executor = _executor(store, _ScriptedDecider([_finish()]), gateway, artifacts)

    await executor.run("task-1")

    assert artifacts.finalized == [("failed", "no_evidence_collected", None)]
    assert store.terminal == "failed:no_evidence_collected"


@pytest.mark.asyncio
async def test_goal_flow_legacy_task_has_zero_behavior_diff() -> None:
    store = _LegacyStore(_task(), goal=None)
    gateway = _FakeGateway([(_settled(),)])
    artifacts = _FakeArtifacts(store)
    selection = _FakeSelection()
    executor = _executor(store, _ScriptedDecider([_call(), _finish("done")]), gateway,
                         artifacts, selection)

    await executor.run("task-1")

    event_types = [event_type for event_type, _ in store.events]
    assert not any(event_type.startswith("goal.") for event_type in event_types)
    assert "artifact.updated" not in event_types
    tool_started = next(payload for t, payload in store.events if t == "tool.started")
    assert "goal_id" not in tool_started
    assert gateway.commands[0].goal_id is None
    assert selection.calls[0]["goal_id"] is None
    assert artifacts.finalized == []
    assert store.terminal == "completed"


# ---------------------------------------------------------------------------
# Part C：_TaskArtifacts.finalize_goal DB 集成
# ---------------------------------------------------------------------------


async def _create_leased_task_with_goal(worker_id: str) -> dict[str, str]:
    """写入持有有效租约的任务 + kol_selection goal（plan_json 非空验证轨迹镜像）。"""
    now = datetime.now(UTC).replace(tzinfo=None)
    ids = {
        "user_id": str(uuid4()),
        "session_id": str(uuid4()),
        "message_id": str(uuid4()),
        "task_id": str(uuid4()),
        "goal_id": str(uuid4()),
    }
    async with SessionFactory.begin() as db:
        db.add(
            User(
                id=ids["user_id"],
                nickname="goal 收尾测试",
                role="user",
                status="active",
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            WorkspaceSession(
                id=ids["session_id"],
                user_id=ids["user_id"],
                title="goal 收尾测试",
                brand="海底捞",
                status="active",
                platforms=["xiaohongshu"],
                category="美食",
                target_audience="",
                last_accessed_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        await db.flush()
        db.add(
            Message(
                id=ids["message_id"],
                session_id=ids["session_id"],
                user_id=ids["user_id"],
                role="user",
                content="帮我圈选达人",
                sequence=1,
                metadata_json={},
                created_at=now,
            )
        )
        await db.flush()
        db.add(
            AnalysisTask(
                id=ids["task_id"],
                user_id=ids["user_id"],
                session_id=ids["session_id"],
                trigger_message_id=ids["message_id"],
                status="running",
                kind="agent",
                plan_json={"agent_trajectory_v1": {"steps": [], "results": []}},
                lease_owner=worker_id,
                lease_expires_at=now + timedelta(minutes=5),
                started_at=now,
                max_calls=10,
                estimated_points=0,
                creation_order=1,
                created_at=now,
                updated_at=now,
            )
        )
        await db.flush()
        db.add(
            TaskGoal(
                id=ids["goal_id"],
                task_id=ids["task_id"],
                sequence=1,
                goal_type="kol_selection",
                status="running",
                params_json={"brand": "海底捞", "category": "美食"},
                started_at=now,
                created_at=now,
                updated_at=now,
            )
        )
    return ids


async def _cleanup(ids: dict[str, str]) -> None:
    async with SessionFactory.begin() as db:
        await db.execute(delete(TaskEvent).where(TaskEvent.task_id == ids["task_id"]))
        await db.execute(delete(TaskArtifact).where(TaskArtifact.goal_id == ids["goal_id"]))
        await db.execute(delete(TaskGoal).where(TaskGoal.id == ids["goal_id"]))
        await db.execute(
            delete(KolSelectionItem).where(
                KolSelectionItem.selection_set_id.in_(
                    select(KolSelectionSet.id).where(
                        KolSelectionSet.session_id == ids["session_id"]
                    )
                )
            )
        )
        await db.execute(
            delete(KolSelectionSet).where(KolSelectionSet.session_id == ids["session_id"])
        )
        await db.execute(delete(AnalysisTask).where(AnalysisTask.id == ids["task_id"]))
        await db.execute(
            delete(AnalysisReport).where(AnalysisReport.session_id == ids["session_id"])
        )
        await db.execute(
            delete(SessionKolSelection).where(
                SessionKolSelection.session_id == ids["session_id"]
            )
        )
        await db.execute(delete(Message).where(Message.session_id == ids["session_id"]))
        await db.execute(delete(WorkspaceSession).where(WorkspaceSession.id == ids["session_id"]))
        await db.execute(delete(User).where(User.id == ids["user_id"]))


def _document() -> ReportDocument:
    return ReportDocument(
        title="KOL 圈选分析",
        conclusion="名单质量良好。",
        blocks=[MetricGridBlock(items=[MetricItem(label="圈选总数", value=2)])],
    )


async def _seed_set_and_report(ids: dict[str, str]) -> tuple[str, str]:
    """建当前 goal 的 selection set（含 1 条 item）与会话级报告，返回 (set_id, report_id)。"""
    async with SessionFactory.begin() as db:
        selection_service = KolSelectionService(db)
        selection_set = await selection_service.ensure_selection_set(
            ids["user_id"],
            ids["session_id"],
            task_id=ids["task_id"],
            goal_id=ids["goal_id"],
            title="海底捞圈选名单",
            scope={"brand": "海底捞"},
        )
        now = datetime.now(UTC).replace(tzinfo=None)
        db.add(
            KolSelectionItem(
                id=str(uuid4()),
                user_id=ids["user_id"],
                selection_set_id=selection_set.id,
                platform="xiaohongshu",
                kol_uid="uid-1",
                nickname="达人1",
                followers=1000,
                fields_json={},
                score_json={"total": 80.0},
                source_tool="tool",
                first_task_id=ids["task_id"],
                last_task_id=ids["task_id"],
                created_at=now,
                updated_at=now,
            )
        )
        report = await AnalysisReportService(db).build_session_report(
            user_id=ids["user_id"],
            session_id=ids["session_id"],
            document=_document(),
            scope={"brand": "海底捞", "category": "美食"},
        )
    return selection_set.id, report.id


@pytest.mark.asyncio
async def test_finalize_goal_completes_set_and_registers_artifacts() -> None:
    worker_id = f"test-worker-{uuid4()}"
    ids = await _create_leased_task_with_goal(worker_id)
    try:
        set_id, report_id = await _seed_set_and_report(ids)
        artifacts = _TaskArtifacts(worker_id, model=None)

        await artifacts.finalize_goal(ids["task_id"], terminal_status="completed")

        async with SessionFactory() as db:
            goal = await db.get(TaskGoal, ids["goal_id"])
            assert goal is not None
            assert goal.status == "completed"
            assert goal.completed_at is not None
            assert goal.error_code is None
            # 轨迹镜像 task.plan_json。
            assert goal.trajectory_json == {"agent_trajectory_v1": {"steps": [], "results": []}}
            selection_set = await db.get(KolSelectionSet, set_id)
            assert selection_set is not None
            assert selection_set.status == "completed"

            artifact_rows = {
                artifact.artifact_key: artifact
                for artifact in (
                    await db.scalars(
                        select(TaskArtifact).where(TaskArtifact.goal_id == ids["goal_id"])
                    )
                ).all()
            }
            set_artifact = artifact_rows[f"goal:{ids['goal_id']}:kol_selection_set"]
            assert set_artifact.artifact_type == "kol_selection_set"
            assert set_artifact.selection_set_id == set_id
            assert set_artifact.report_id is None
            assert set_artifact.title == "海底捞圈选名单"
            assert set_artifact.version == 1
            assert set_artifact.status == "completed"
            assert set_artifact.task_id == ids["task_id"]
            assert set_artifact.scope_json == {"brand": "海底捞"}
            report_artifact = artifact_rows[f"goal:{ids['goal_id']}:kol_report"]
            assert report_artifact.artifact_type == "kol_report"
            assert report_artifact.report_id == report_id
            assert report_artifact.version == 1
            assert report_artifact.scope_json == {"brand": "海底捞", "category": "美食"}

            events = list(
                (
                    await db.scalars(
                        select(TaskEvent)
                        .where(TaskEvent.task_id == ids["task_id"])
                        .order_by(TaskEvent.id)
                    )
                ).all()
            )
            artifact_events = [e for e in events if e.event_type == "artifact.updated"]
            assert len(artifact_events) == 2
            by_type = {e.payload_json["artifact_type"]: e.payload_json for e in artifact_events}
            set_payload = by_type["kol_selection_set"]
            assert set_payload["artifact_id"] == set_artifact.id
            assert set_payload["goal_id"] == ids["goal_id"]
            assert set_payload["module_key"] == "kol_selection"
            assert set_payload["version"] == 1
            assert set_payload["title"] == "海底捞圈选名单"
            assert by_type["kol_report"]["module_key"] == "kol_analysis"
            goal_events = [e for e in events if e.event_type == "goal.completed"]
            assert len(goal_events) == 1
            assert goal_events[0].payload_json == {
                "goal_id": ids["goal_id"],
                "goal_type": "kol_selection",
                "status": "completed",
            }
    finally:
        await _cleanup(ids)


@pytest.mark.asyncio
async def test_finalize_goal_replay_does_not_duplicate_artifacts() -> None:
    worker_id = f"test-worker-{uuid4()}"
    ids = await _create_leased_task_with_goal(worker_id)
    try:
        await _seed_set_and_report(ids)
        artifacts = _TaskArtifacts(worker_id, model=None)

        await artifacts.finalize_goal(ids["task_id"], terminal_status="completed")
        # 崩溃恢复重放收尾段：artifact_key 幂等，不重复建行、不重复发事件。
        await artifacts.finalize_goal(ids["task_id"], terminal_status="completed")

        async with SessionFactory() as db:
            total = await db.scalar(
                select(func.count())
                .select_from(TaskArtifact)
                .where(TaskArtifact.goal_id == ids["goal_id"])
            )
            assert total == 2
            event_count = await db.scalar(
                select(func.count())
                .select_from(TaskEvent)
                .where(
                    TaskEvent.task_id == ids["task_id"],
                    TaskEvent.event_type == "artifact.updated",
                )
            )
            assert event_count == 2
    finally:
        await _cleanup(ids)


@pytest.mark.asyncio
async def test_finalize_goal_insufficient_balance_keeps_artifacts() -> None:
    worker_id = f"test-worker-{uuid4()}"
    ids = await _create_leased_task_with_goal(worker_id)
    try:
        await _seed_set_and_report(ids)
        artifacts = _TaskArtifacts(worker_id, model=None)

        await artifacts.finalize_goal(ids["task_id"], terminal_status="insufficient_balance")

        async with SessionFactory() as db:
            goal = await db.get(TaskGoal, ids["goal_id"])
            assert goal is not None
            assert goal.status == "insufficient_balance"
            total = await db.scalar(
                select(func.count())
                .select_from(TaskArtifact)
                .where(TaskArtifact.goal_id == ids["goal_id"])
            )
            # 已产生的 set 与报告 Artifact 保留。
            assert total == 2
            goal_event = await db.scalar(
                select(TaskEvent).where(
                    TaskEvent.task_id == ids["task_id"],
                    TaskEvent.event_type == "goal.completed",
                )
            )
            assert goal_event is not None
            assert goal_event.payload_json["status"] == "insufficient_balance"
    finally:
        await _cleanup(ids)


@pytest.mark.asyncio
async def test_finalize_goal_failed_emits_goal_failed() -> None:
    worker_id = f"test-worker-{uuid4()}"
    ids = await _create_leased_task_with_goal(worker_id)
    try:
        artifacts = _TaskArtifacts(worker_id, model=None)

        await artifacts.finalize_goal(
            ids["task_id"], terminal_status="failed", error_code="no_evidence_collected"
        )

        async with SessionFactory() as db:
            goal = await db.get(TaskGoal, ids["goal_id"])
            assert goal is not None
            assert goal.status == "failed"
            assert goal.error_code == "no_evidence_collected"
            assert goal.completed_at is not None
            # 零证据：无 set、无报告 → 不登记任何 Artifact。
            total = await db.scalar(
                select(func.count())
                .select_from(TaskArtifact)
                .where(TaskArtifact.goal_id == ids["goal_id"])
            )
            assert total == 0
            goal_event = await db.scalar(
                select(TaskEvent).where(
                    TaskEvent.task_id == ids["task_id"],
                    TaskEvent.event_type == "goal.failed",
                )
            )
            assert goal_event is not None
            assert goal_event.payload_json == {
                "goal_id": ids["goal_id"],
                "goal_type": "kol_selection",
                "status": "failed",
                "error_code": "no_evidence_collected",
            }
    finally:
        await _cleanup(ids)
