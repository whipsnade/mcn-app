"""executor 多 Goal 顺序编排（轨迹 v2）：事件、依赖、恢复、终态聚合。"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from sqlalchemy import delete

from app.billing.service import InsufficientPointsError
from app.db.session import SessionFactory
from app.identity.models import User
from app.mcp_gateway.contracts import DataTapService
from app.orchestration.loop import AgentDecision, AgentLoopContext
from app.orchestration.schemas import PlannerTool
from app.tasks.dependencies import _PlanArguments
from app.tasks.executor import TaskExecutor
from app.tasks.models import AnalysisTask
from app.workspace.models import Message, WorkspaceSession


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


def _call(keyword: str = "美妆") -> AgentDecision:
    return AgentDecision(
        action="call_tool",
        internal_tool_name=_TOOL_NAME,
        arguments={"keyword": keyword},
        evidence_goal="声量概览",
    )


def _finish(conclusion: str = "") -> AgentDecision:
    return AgentDecision(action="finish", conclusion=conclusion)


def _settled(keyword: str = "美妆") -> SimpleNamespace:
    return SimpleNamespace(
        status="settled",
        internal_tool_name=_TOOL_NAME,
        plan_step_id="step_1",
        evidence_json={"structured_content": {"keyword": keyword, "total_volume": 12345}},
        error_type=None,
    )


def _goal(
    goal_id: str,
    goal_type: str,
    sequence: int,
    *,
    params: dict | None = None,
    status: str = "pending",
    depends_on_goal_id: str | None = None,
    result_summary_json: dict | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=goal_id,
        goal_type=goal_type,
        sequence=sequence,
        params_json=params or {},
        status=status,
        depends_on_goal_id=depends_on_goal_id,
        result_summary_json=result_summary_json,
    )


def _task() -> SimpleNamespace:
    return SimpleNamespace(
        id="task-1",
        user_id="user-1",
        session_id="session-1",
        kind="agent",
        plan_json=None,
        retry_of_task_id=None,
    )


class _MultiGoalStore:
    def __init__(self, task: SimpleNamespace, goals: list[SimpleNamespace]) -> None:
        self.task = task
        self.goals = goals
        self.events: list[tuple[str, dict]] = []
        self.saved_payloads: list[dict] = []
        self.terminal: str | None = None

    async def claim_lease(self, task_id, worker_id, lease_seconds):
        return self.task

    async def save_plan(self, task_id, worker_id, plan_json):
        self.task.plan_json = plan_json
        self.saved_payloads.append(plan_json)
        return True

    async def save_trajectory(self, task_id, worker_id, trajectory_json):
        self.task.plan_json = trajectory_json
        self.saved_payloads.append(trajectory_json)
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

    async def get_task_goals(self, task_id):
        return self.goals


class _FakeArtifacts:
    def __init__(self, store: _MultiGoalStore) -> None:
        self._store = store
        self.finalized: list[dict] = []
        self.conclusions: list[str] = []
        self.auto_analysis_calls = 0

    async def write_conclusion_message(self, task_id, conclusion):
        self.conclusions.append(conclusion)

    async def auto_kol_analysis(self, task_id):
        self.auto_analysis_calls += 1

    async def finalize_goal(
        self, task_id, *, goal_id, terminal_status, error_code=None, warning_code=None
    ):
        self.finalized.append(
            {
                "goal_id": goal_id,
                "status": terminal_status,
                "error_code": error_code,
                "warning_code": warning_code,
            }
        )
        # 模拟生产 finalize 的落库效果：状态与摘要回写到 goal 行，goal.* 事件照发。
        for goal in self._store.goals:
            if goal.id == goal_id:
                goal.status = terminal_status
                payload = {
                    "goal_id": goal.id,
                    "goal_type": goal.goal_type,
                    "status": terminal_status,
                }
                if error_code:
                    payload["error_code"] = error_code
                event_type = (
                    "goal.failed"
                    if terminal_status in {"failed", "skipped"}
                    else "goal.completed"
                )
                self._store.events.append((event_type, payload))


class _FakeContextBuilder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def build_agent_context(self, user_id, session_id, **kwargs):
        self.calls.append(kwargs)
        return AgentLoopContext(
            recent_messages=(),
            tools=(_tool(),),
            allowed_channels=("xiaohongshu",),
        )


class _ScriptedDecider:
    def __init__(self, decisions: list[AgentDecision]) -> None:
        self._decisions = list(decisions)
        self.contexts: list[AgentLoopContext] = []

    async def agent_decide(self, context):
        self.contexts.append(context)
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


class _FakeSelection:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def ingest(self, **kwargs):
        self.calls.append(kwargs)


def _executor(store, decider, gateway, artifacts, selection=None, context_builder=None) -> TaskExecutor:
    return TaskExecutor(
        repository=store,
        context_builder=context_builder or _FakeContextBuilder(),
        planner=decider,
        gateway=gateway,
        artifacts=artifacts,
        selection=selection,
        worker_id="worker-1",
        lease_seconds=60,
        heartbeat_seconds=0.05,
    )


@pytest.mark.asyncio
async def test_two_goals_run_sequentially_with_namespaced_steps() -> None:
    goals = [
        _goal("goal-campaign", "campaign_analysis", 1, params={"brand": "海底捞", "campaign": "618"}),
        _goal("goal-kol", "kol_selection", 2, params={"brand": "海底捞"}, depends_on_goal_id="goal-campaign"),
    ]
    store = _MultiGoalStore(_task(), goals)
    gateway = _FakeGateway([(_settled(),), (_settled(),)])
    artifacts = _FakeArtifacts(store)
    selection = _FakeSelection()
    decider = _ScriptedDecider([_call(), _finish("活动复盘完成"), _call(), _finish("圈选完成")])
    executor = _executor(store, decider, gateway, artifacts, selection)

    await executor.run("task-1")

    # goal.started ×2，各自 goal_id；工具事件 goal_id 按 goal 切换。
    started = [payload for event, payload in store.events if event == "goal.started"]
    assert [payload["goal_id"] for payload in started] == ["goal-campaign", "goal-kol"]
    tool_started = [payload for event, payload in store.events if event == "tool.started"]
    assert [payload["goal_id"] for payload in tool_started] == ["goal-campaign", "goal-kol"]
    assert [command.goal_id for command in gateway.commands] == ["goal-campaign", "goal-kol"]
    # step id 命名空间 g{seq}_step_N，logical_call_id 天然唯一。
    assert gateway.commands[0].plan_step_id == "g1_step_1"
    assert gateway.commands[1].plan_step_id == "g2_step_1"
    assert gateway.commands[0].logical_call_id == str(
        uuid5(NAMESPACE_URL, "task-1:g1_step_1")
    )
    assert gateway.commands[1].logical_call_id == str(
        uuid5(NAMESPACE_URL, "task-1:g2_step_1")
    )
    # 轨迹 v2：各 goal 独立切片。
    final_payload = store.saved_payloads[-1]
    assert final_payload["schema"] == "agent_trajectory_v2"
    assert set(final_payload["goals"]) == {"goal-campaign", "goal-kol"}
    assert final_payload["goals"]["goal-campaign"]["steps"][0]["id"] == "g1_step_1"
    assert final_payload["goals"]["goal-kol"]["steps"][0]["id"] == "g2_step_1"
    # 各自 finalize：campaign completed、kol completed；ingest 仅 kol 触发。
    assert [entry["status"] for entry in artifacts.finalized] == ["completed", "completed"]
    assert [entry["goal_id"] for entry in artifacts.finalized] == ["goal-campaign", "goal-kol"]
    assert len(selection.calls) == 1
    assert selection.calls[0]["goal_id"] == "goal-kol"
    assert artifacts.auto_analysis_calls == 1
    assert store.terminal == "completed"


@pytest.mark.asyncio
async def test_dependency_summary_injected_into_downstream_context() -> None:
    upstream = _goal(
        "goal-brand",
        "brand_analysis",
        1,
        params={"brand": "海底捞"},
        result_summary_json={"summary": "海底捞声量上涨", "highlights": {}, "artifact": None},
    )
    goals = [
        upstream,
        _goal("goal-kol", "kol_selection", 2, params={"brand": "海底捞"}, depends_on_goal_id="goal-brand"),
    ]
    store = _MultiGoalStore(_task(), goals)
    gateway = _FakeGateway([(_settled(),), (_settled(),)])

    class _SummarizingArtifacts(_FakeArtifacts):
        async def finalize_goal(self, task_id, *, goal_id, terminal_status, error_code=None, warning_code=None):
            await super().finalize_goal(
                task_id,
                goal_id=goal_id,
                terminal_status=terminal_status,
                error_code=error_code,
                warning_code=warning_code,
            )
            for goal in self._store.goals:
                if goal.id == goal_id and terminal_status == "completed":
                    goal.result_summary_json = {
                        "summary": "海底捞声量上涨",
                        "highlights": {},
                        "artifact": None,
                    }

    artifacts = _SummarizingArtifacts(store)
    decider = _ScriptedDecider([_call(), _finish("品牌分析完成"), _call(), _finish("圈选完成")])
    executor = _executor(store, decider, gateway, artifacts, _FakeSelection())

    await executor.run("task-1")

    # 下游 context 的 dependency_summaries 含上游摘要。
    downstream_contexts = [
        context for context in decider.contexts if context.dependency_summaries
    ]
    assert downstream_contexts
    assert downstream_contexts[0].dependency_summaries[0]["summary"] == "海底捞声量上涨"


@pytest.mark.asyncio
async def test_soft_dependency_failed_upstream_still_runs_with_warning() -> None:
    goals = [
        _goal("goal-brand", "brand_analysis", 1, params={"brand": "海底捞"}),
        _goal("goal-kol", "kol_selection", 2, params={"brand": "海底捞"}, depends_on_goal_id="goal-brand"),
    ]
    store = _MultiGoalStore(_task(), goals)
    # 上游零证据 finish → failed；下游照常执行并记 dependency_missing。
    gateway = _FakeGateway([(_settled(),)])
    artifacts = _FakeArtifacts(store)
    decider = _ScriptedDecider([_finish("没有证据"), _call(), _finish("圈选完成")])
    executor = _executor(store, decider, gateway, artifacts, _FakeSelection())

    await executor.run("task-1")

    assert artifacts.finalized[0]["goal_id"] == "goal-brand"
    assert artifacts.finalized[0]["status"] == "failed"
    assert artifacts.finalized[0]["error_code"] == "no_evidence_collected"
    kol_finalize = artifacts.finalized[1]
    assert kol_finalize["goal_id"] == "goal-kol"
    assert kol_finalize["status"] == "completed"
    assert kol_finalize["warning_code"] == "dependency_missing"
    assert len(gateway.commands) == 1
    assert store.terminal == "completed_with_warnings:goals_partial_failure"


@pytest.mark.asyncio
async def test_soft_dependency_kol_without_brand_is_skipped() -> None:
    goals = [
        _goal("goal-brand", "brand_analysis", 1, params={"brand": "海底捞"}),
        _goal("goal-kol", "kol_selection", 2, params={}, depends_on_goal_id="goal-brand"),
    ]
    store = _MultiGoalStore(_task(), goals)
    gateway = _FakeGateway([])
    artifacts = _FakeArtifacts(store)
    decider = _ScriptedDecider([_finish("没有证据")])
    executor = _executor(store, decider, gateway, artifacts, _FakeSelection())

    await executor.run("task-1")

    # 上游失败 + 下游 kol 缺 brand：skipped 不执行循环。
    assert gateway.commands == []
    kol_finalize = artifacts.finalized[1]
    assert kol_finalize["goal_id"] == "goal-kol"
    assert kol_finalize["status"] == "skipped"
    assert kol_finalize["error_code"] == "dependency_missing_brand"
    skipped_events = [
        payload for event, payload in store.events if event == "goal.failed"
    ]
    assert any(payload["goal_id"] == "goal-kol" for payload in skipped_events)


@pytest.mark.asyncio
async def test_insufficient_balance_stops_orchestration() -> None:
    goals = [
        _goal("goal-brand", "brand_analysis", 1, params={"brand": "海底捞"}),
        _goal("goal-kol", "kol_selection", 2, params={"brand": "海底捞"}),
        _goal("goal-campaign", "campaign_analysis", 3, params={"brand": "海底捞", "campaign": "618"}),
    ]
    store = _MultiGoalStore(_task(), goals)
    gateway = _FakeGateway([(_settled(),), InsufficientPointsError("余额不足")])
    artifacts = _FakeArtifacts(store)
    decider = _ScriptedDecider([_call(), _finish("品牌分析完成"), _call()])
    executor = _executor(store, decider, gateway, artifacts, _FakeSelection())

    await executor.run("task-1")

    assert artifacts.finalized[-1]["goal_id"] == "goal-kol"
    assert artifacts.finalized[-1]["status"] == "insufficient_balance"
    # 后续 goal 保持 pending，任务进入 insufficient_balance。
    assert goals[2].status == "pending"
    assert store.terminal == "insufficient_balance"


@pytest.mark.asyncio
async def test_resume_skips_terminal_goal_and_replays_pending_step() -> None:
    goals = [
        _goal("goal-brand", "brand_analysis", 1, params={"brand": "海底捞"}, status="completed",
              result_summary_json={"summary": "已完成", "highlights": {}, "artifact": None}),
        _goal("goal-kol", "kol_selection", 2, params={"brand": "海底捞"}, status="running"),
    ]
    task = _task()
    task.plan_json = {
        "schema": "agent_trajectory_v2",
        "goals": {
            "goal-brand": {"steps": [], "results": []},
            "goal-kol": {
                "steps": [
                    {
                        "id": "g2_step_1",
                        "internal_tool_name": _TOOL_NAME,
                        "arguments": {"keyword": "美妆"},
                        "evidence_goal": "声量概览",
                    },
                    {
                        "id": "g2_step_2",
                        "internal_tool_name": _TOOL_NAME,
                        "arguments": {"keyword": "海底捞"},
                        "evidence_goal": "品牌声量",
                    },
                ],
                "results": [
                    {
                        "step_id": "g2_step_1",
                        "tool": _TOOL_NAME,
                        "status": "settled",
                        "summary": {"total_volume": 12345},
                    }
                ],
            },
        },
    }
    store = _MultiGoalStore(task, goals)
    gateway = _FakeGateway([(_settled("海底捞"),)])
    artifacts = _FakeArtifacts(store)
    decider = _ScriptedDecider([_finish("圈选完成")])
    executor = _executor(store, decider, gateway, artifacts, _FakeSelection())

    await executor.run("task-1")

    # goal1 终态跳过：只发一次 goal.started（goal-kol）。
    started = [payload for event, payload in store.events if event == "goal.started"]
    assert [payload["goal_id"] for payload in started] == ["goal-kol"]
    # 断点续跑：直接重放 g2_step_2 的原始参数（本轮不问模型）。
    assert len(gateway.commands) == 1
    command = gateway.commands[0]
    assert command.plan_step_id == "g2_step_2"
    assert command.arguments == {"keyword": "海底捞"}
    assert command.logical_call_id == str(uuid5(NAMESPACE_URL, "task-1:g2_step_2"))
    assert store.terminal == "completed"


@pytest.mark.asyncio
async def test_terminal_aggregation_all_failed_marks_all_goals_failed() -> None:
    goals = [
        _goal("goal-brand", "brand_analysis", 1, params={"brand": "海底捞"}),
        _goal("goal-campaign", "campaign_analysis", 2, params={"brand": "海底捞", "campaign": "618"}),
    ]
    store = _MultiGoalStore(_task(), goals)
    gateway = _FakeGateway([])
    artifacts = _FakeArtifacts(store)
    decider = _ScriptedDecider([_finish("没有证据"), _finish("还是没有")])
    executor = _executor(store, decider, gateway, artifacts, _FakeSelection())

    await executor.run("task-1")

    assert [entry["status"] for entry in artifacts.finalized] == ["failed", "failed"]
    assert store.terminal == "failed:all_goals_failed"


# ---------------------------------------------------------------------------
# _PlanArguments：v2 切片查参数
# ---------------------------------------------------------------------------


async def _create_task_with_plan(plan_json: dict) -> str:
    now = datetime.now(UTC).replace(tzinfo=None)
    ids = {
        "user_id": str(uuid4()),
        "session_id": str(uuid4()),
        "message_id": str(uuid4()),
        "task_id": str(uuid4()),
    }
    async with SessionFactory.begin() as db:
        db.add(
            User(
                id=ids["user_id"], nickname="参数重放", role="user", status="active",
                created_at=now, updated_at=now,
            )
        )
        db.add(
            WorkspaceSession(
                id=ids["session_id"], user_id=ids["user_id"], title="参数重放",
                brand="海底捞", status="active", platforms=["xiaohongshu"],
                category="美食", target_audience="", last_accessed_at=now,
                created_at=now, updated_at=now,
            )
        )
        await db.flush()
        db.add(
            Message(
                id=ids["message_id"], session_id=ids["session_id"], user_id=ids["user_id"],
                role="user", content="分析", sequence=1, metadata_json={}, created_at=now,
            )
        )
        await db.flush()
        db.add(
            AnalysisTask(
                id=ids["task_id"], user_id=ids["user_id"], session_id=ids["session_id"],
                trigger_message_id=ids["message_id"], status="running", kind="agent",
                plan_json=plan_json, max_calls=10, estimated_points=0, creation_order=1,
                created_at=now, updated_at=now,
            )
        )
    return ids["task_id"]


async def _cleanup_task(task_id: str) -> None:
    async with SessionFactory.begin() as db:
        task = await db.get(AnalysisTask, task_id)
        if task is None:
            return
        session_id = task.session_id
        user_id = task.user_id
        await db.execute(delete(AnalysisTask).where(AnalysisTask.id == task_id))
        await db.execute(delete(Message).where(Message.session_id == session_id))
        await db.execute(delete(WorkspaceSession).where(WorkspaceSession.id == session_id))
        await db.execute(delete(User).where(User.id == user_id))


@pytest.mark.asyncio
async def test_plan_arguments_loads_from_v2_slices() -> None:
    plan_json = {
        "schema": "agent_trajectory_v2",
        "goals": {
            "goal-a": {
                "steps": [
                    {"id": "g1_step_1", "internal_tool_name": "tool.a", "arguments": {"keyword": "美妆"}}
                ],
                "results": [],
            },
            "goal-b": {
                "steps": [
                    {"id": "g2_step_1", "internal_tool_name": "tool.a", "arguments": {"keyword": "海底捞"}}
                ],
                "results": [],
            },
        },
    }
    task_id = await _create_task_with_plan(plan_json)
    try:
        loader = _PlanArguments()
        args1 = await loader.load_arguments(task_id=task_id, plan_step_id="g2_step_1")
        assert args1 == {"keyword": "海底捞"}
        args2 = await loader.load_arguments(task_id=task_id, plan_step_id="g1_step_1")
        assert args2 == {"keyword": "美妆"}
        with pytest.raises(LookupError):
            await loader.load_arguments(task_id=task_id, plan_step_id="g9_step_9")
    finally:
        await _cleanup_task(task_id)


@pytest.mark.asyncio
async def test_plan_arguments_v1_still_works() -> None:
    plan_json = {
        "schema": "agent_trajectory_v1",
        "steps": [
            {"id": "step_1", "internal_tool_name": "tool.a", "arguments": {"keyword": "美妆"}}
        ],
        "results": [],
    }
    task_id = await _create_task_with_plan(plan_json)
    try:
        loader = _PlanArguments()
        args = await loader.load_arguments(task_id=task_id, plan_step_id="step_1")
        assert args == {"keyword": "美妆"}
    finally:
        await _cleanup_task(task_id)
