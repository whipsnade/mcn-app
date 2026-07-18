from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest

from app.mcp_gateway.contracts import DataTapService
from app.orchestration.loop import (
    AgentDecision,
    AgentLoopContext,
    AgentTrajectory,
    EvidenceNote,
    TrajectoryStep,
)
from app.orchestration.schemas import PlannerTool
from app.tasks.executor import TaskExecutor


_TOOL_NAME = "datatap.insight.social.statistic.overview.v1"
_TOOL_SCHEMA = {
    "type": "object",
    "properties": {"keyword": {"type": "string"}},
    "required": ["keyword"],
    "additionalProperties": False,
}
_STAT_TOOL_NAME = "datatap.insight.query.analysis.v1"
_STAT_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "datasource": {"type": "array", "items": {"type": "string"}},
        "start_time": {"type": "string"},
        "end_time": {"type": "string"},
        "target_type": {"type": "string"},
    },
    "required": ["name", "datasource", "start_time", "end_time", "target_type"],
    "additionalProperties": False,
}


def _stat_tool() -> PlannerTool:
    return PlannerTool(
        catalog_id="cat-2",
        internal_name=_STAT_TOOL_NAME,
        service=DataTapService.INSIGHT_CUBE,
        description="分析查询",
        input_schema=_STAT_TOOL_SCHEMA,
        output_schema={},
    )


def _tool() -> PlannerTool:
    return PlannerTool(
        catalog_id="cat-1",
        internal_name=_TOOL_NAME,
        service=DataTapService.INSIGHT_CUBE,
        description="声量概览",
        input_schema=_TOOL_SCHEMA,
        output_schema={},
    )


def _call(arguments: dict | None = None, tool: str = _TOOL_NAME) -> AgentDecision:
    return AgentDecision(
        action="call_tool",
        internal_tool_name=tool,
        arguments={"keyword": "美妆"} if arguments is None else arguments,
        evidence_goal="声量概览",
    )


def _finish() -> AgentDecision:
    return AgentDecision(action="finish")


def _settled(data: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        status="settled",
        internal_tool_name=_TOOL_NAME,
        plan_step_id="step_1",
        evidence_json={"structured_content": {"total_volume": 12345} if data is None else data},
        error_type=None,
    )


def _released() -> SimpleNamespace:
    return SimpleNamespace(
        status="released",
        internal_tool_name=_TOOL_NAME,
        plan_step_id="step_1",
        evidence_json={"outcome": "failed"},
        error_type="upstream_tool_error",
    )


class _FakeStore:
    def __init__(self, task: SimpleNamespace) -> None:
        self.task = task
        self.events: list[tuple[str, dict]] = []
        self.trajectories: list[dict] = []
        self.terminal: str | None = None

    async def claim_lease(self, task_id, worker_id, lease_seconds):
        return self.task

    async def save_plan(self, task_id, worker_id, plan_json):
        self.task.plan_json = plan_json
        self.trajectories.append(plan_json)
        return True

    async def save_trajectory(self, task_id, worker_id, trajectory_json):
        self.task.plan_json = trajectory_json
        self.trajectories.append(trajectory_json)
        return True

    async def save_replan(self, task_id, worker_id, replan_json):
        return True

    async def cancel_requested(self, task_id):
        return False

    async def renew_lease(self, task_id, worker_id, lease_seconds):
        return True

    async def mark_completed(self, task_id, worker_id):
        self.terminal = "completed"

    async def mark_completed_with_warnings(self, task_id, worker_id, code, message=None):
        self.terminal = f"completed_with_warnings:{code}"

    async def mark_cancelled(self, task_id, worker_id):
        self.terminal = "cancelled"

    async def mark_interrupted(self, task_id, worker_id):
        self.terminal = "interrupted"

    async def mark_failed(self, task_id, worker_id, code, message=None):
        self.terminal = f"failed:{code}"

    async def append_event(self, task_id, user_id, event_type, payload):
        self.events.append((str(event_type), payload))

    async def release_lease(self, task_id, worker_id):
        return None


class _FakeContextBuilder:
    async def build(self, user_id, session_id):  # pragma: no cover - pipeline 路径
        raise AssertionError("pipeline context must not be built for agent tasks")

    async def build_agent_context(self, user_id, session_id):
        return AgentLoopContext(
            recent_messages=(),
            tools=(_tool(), _stat_tool()),
            allowed_channels=("xiaohongshu", "douyin"),
        )


class _ScriptedDecider:
    def __init__(self, decisions: list[AgentDecision]) -> None:
        self._decisions = list(decisions)
        self.calls = 0

    async def agent_decide(self, context):
        self.calls += 1
        return self._decisions.pop(0)


class _FakeGateway:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = list(rows)
        self.commands: list[tuple] = []

    async def execute_batch(self, commands):
        self.commands.append(commands)
        return self._rows.pop(0)


class _FakeArtifacts:
    def __init__(self) -> None:
        self.built: list[str] = []
        self.streamed: list[str] = []

    async def build_analysis_report(self, task_id):
        self.built.append(task_id)

    async def stream_analysis_summary(self, task_id):
        self.streamed.append(task_id)


def _task(max_calls: int = 10, plan_json: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id="task-1",
        user_id="user-1",
        session_id="session-1",
        kind="agent",
        plan_json=plan_json,
        max_calls=max_calls,
    )


def _executor(store, decider, gateway, artifacts) -> TaskExecutor:
    return TaskExecutor(
        repository=store,
        context_builder=_FakeContextBuilder(),
        planner=decider,
        gateway=gateway,
        artifacts=artifacts,
        worker_id="worker-1",
        lease_seconds=60,
        heartbeat_seconds=0.05,
    )


@pytest.mark.asyncio
async def test_agent_loop_calls_tools_until_finish_then_builds_report() -> None:
    task = _task()
    store = _FakeStore(task)
    decider = _ScriptedDecider([_call(), _call(), _finish()])
    gateway = _FakeGateway([(_settled(),), (_settled(),)])
    artifacts = _FakeArtifacts()

    await _executor(store, decider, gateway, artifacts).run(task.id)

    assert store.terminal == "completed"
    assert artifacts.built == [task.id]
    assert artifacts.streamed == [task.id]
    assert len(gateway.commands) == 2
    assert gateway.commands[0][0].logical_call_id == str(uuid5(NAMESPACE_URL, "task-1:step_1"))
    assert gateway.commands[1][0].logical_call_id == str(uuid5(NAMESPACE_URL, "task-1:step_2"))
    event_types = [event for event, _payload in store.events]
    assert event_types.count("tool.started") == 2
    assert event_types.count("tool.succeeded") == 2
    final = store.trajectories[-1]
    assert final["schema"] == "agent_trajectory_v1"
    assert len(final["results"]) == 2


@pytest.mark.asyncio
async def test_agent_loop_stops_at_call_budget_without_finish() -> None:
    task = _task(max_calls=2)
    store = _FakeStore(task)
    decider = _ScriptedDecider([_call(), _call()])
    gateway = _FakeGateway([(_settled(),), (_settled(),)])
    artifacts = _FakeArtifacts()

    await _executor(store, decider, gateway, artifacts).run(task.id)

    assert store.terminal == "completed"
    assert len(gateway.commands) == 2
    assert decider.calls == 2


@pytest.mark.asyncio
async def test_agent_loop_failed_call_feeds_back_and_marks_warnings() -> None:
    task = _task()
    store = _FakeStore(task)
    decider = _ScriptedDecider([_call(), _call(), _finish()])
    gateway = _FakeGateway([(_released(),), (_settled(),)])
    artifacts = _FakeArtifacts()

    await _executor(store, decider, gateway, artifacts).run(task.id)

    assert store.terminal == "completed_with_warnings:mcp_partial_failure"
    final = store.trajectories[-1]
    statuses = [note["status"] for note in final["results"]]
    assert statuses == ["failed", "settled"]


@pytest.mark.asyncio
async def test_agent_loop_failure_note_includes_upstream_hint() -> None:
    task = _task()
    store = _FakeStore(task)
    upstream_hint = '分析对象校验失败: 标签名称 "美妆" 不在列表中。建议使用 match_best_tag。'
    released_with_hint = SimpleNamespace(
        status="released",
        internal_tool_name=_TOOL_NAME,
        plan_step_id="step_1",
        evidence_json={"outcome": "failed", "upstream_error_message": upstream_hint},
        error_type="upstream_tool_error",
    )
    decider = _ScriptedDecider([_call(), _call(), _finish()])
    gateway = _FakeGateway([(released_with_hint,), (_settled(),)])
    artifacts = _FakeArtifacts()

    await _executor(store, decider, gateway, artifacts).run(task.id)

    final = store.trajectories[-1]
    failed_note = next(note for note in final["results"] if note["status"] == "failed")
    assert "上游提示" in failed_note["summary"]
    assert "match_best_tag" in failed_note["summary"]


@pytest.mark.asyncio
async def test_agent_loop_unknown_call_interrupts_without_report() -> None:
    task = _task()
    store = _FakeStore(task)
    unknown = SimpleNamespace(
        status="unknown",
        internal_tool_name=_TOOL_NAME,
        plan_step_id="step_1",
        evidence_json={"outcome": "unknown"},
        error_type="possibly_sent_timeout",
    )
    decider = _ScriptedDecider([_call()])
    gateway = _FakeGateway([(unknown,)])
    artifacts = _FakeArtifacts()

    await _executor(store, decider, gateway, artifacts).run(task.id)

    assert store.terminal == "interrupted"
    assert artifacts.built == []


@pytest.mark.asyncio
async def test_agent_loop_without_any_evidence_fails() -> None:
    task = _task(max_calls=2)
    store = _FakeStore(task)
    decider = _ScriptedDecider([_call(), _call()])
    gateway = _FakeGateway([(_released(),), (_released(),)])
    artifacts = _FakeArtifacts()

    await _executor(store, decider, gateway, artifacts).run(task.id)

    assert store.terminal == "failed:mcp_call_failed"
    assert artifacts.built == []


@pytest.mark.asyncio
async def test_agent_loop_repeated_invalid_decisions_fail_fast() -> None:
    task = _task()
    store = _FakeStore(task)
    decider = _ScriptedDecider(
        [_call(tool="datatap.unknown.tool"), _call(tool="datatap.unknown.tool")]
    )
    gateway = _FakeGateway([])
    artifacts = _FakeArtifacts()

    await _executor(store, decider, gateway, artifacts).run(task.id)

    assert store.terminal == "failed:TOOL_NOT_ALLOWED"
    assert gateway.commands == []


@pytest.mark.asyncio
async def test_agent_loop_replays_pending_step_with_original_arguments() -> None:
    trajectory = AgentTrajectory(
        steps=[
            TrajectoryStep(
                id="step_1",
                internal_tool_name=_TOOL_NAME,
                arguments={"keyword": "美妆"},
                evidence_goal="声量",
            ),
            TrajectoryStep(
                id="step_2",
                internal_tool_name=_TOOL_NAME,
                arguments={"keyword": "护肤"},
                evidence_goal="声量",
            ),
        ],
        results=[
            EvidenceNote(
                step_id="step_1",
                tool=_TOOL_NAME,
                status="settled",
                summary={"total_volume": 1},
            )
        ],
    )
    task = _task(plan_json=trajectory.as_plan_json())
    store = _FakeStore(task)
    decider = _ScriptedDecider([_finish()])
    gateway = _FakeGateway([(_settled(),)])
    artifacts = _FakeArtifacts()

    await _executor(store, decider, gateway, artifacts).run(task.id)

    # 崩溃前已持久化的 step_2 按原始参数重放，不再询问模型。
    assert len(gateway.commands) == 1
    assert gateway.commands[0][0].plan_step_id == "step_2"
    assert gateway.commands[0][0].arguments == {"keyword": "护肤"}
    assert decider.calls == 1
    assert store.terminal == "completed"


@pytest.mark.asyncio
async def test_agent_loop_normalizes_model_arguments_before_invoking() -> None:
    task = _task()
    store = _FakeStore(task)
    messy = AgentDecision(
        action="call_tool",
        internal_tool_name=_STAT_TOOL_NAME,
        arguments={
            "name": "格力",
            "datasource": ["douyin", "bilibili"],
            "start_time": "2025-01-01 00:00:00",
            "end_time": "2026-07-18 23:59:59",
            "target_type": "tag",
            # 模型常见的未声明附加字段，应在调用前剔除而不是判失败。
            "metrics": ["声量", "互动数"],
        },
        evidence_goal="声量",
    )
    decider = _ScriptedDecider([messy, _finish()])
    gateway = _FakeGateway([(_settled(),)])
    artifacts = _FakeArtifacts()

    await _executor(store, decider, gateway, artifacts).run(task.id)

    assert store.terminal == "completed"
    arguments = gateway.commands[0][0].arguments
    assert "metrics" not in arguments
    assert arguments["datasource"] == ["短视频__抖音", "视频__哔哩哔哩"]
    assert arguments["start_time"] == "2025-07-18 23:59:59"
    # 持久化轨迹与网关实参一致（恢复重放依赖这一点）
    assert store.trajectories[-1]["steps"][0]["arguments"] == arguments
