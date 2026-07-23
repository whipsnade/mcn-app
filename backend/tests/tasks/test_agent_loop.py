from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from sqlalchemy import delete, select

from app.billing.service import InsufficientPointsError
from app.db.session import SessionFactory
from app.identity.models import User
from app.mcp_gateway.contracts import DataTapService
from app.model.contracts import ModelPlanInvalidError, StructuredResult
from app.reporting.blocks import MetricGridBlock, MetricItem, ReportDocument
from app.reporting.models import AnalysisReport
from app.orchestration.loop import (
    AgentDecision,
    AgentLoopContext,
    AgentTrajectory,
    EvidenceNote,
    TrajectoryStep,
)
from app.orchestration.schemas import PlannerTool
from app.selection.models import SessionKolSelection
from app.tasks.dependencies import _TaskArtifacts
from app.tasks.executor import TaskExecutor
from app.tasks.models import AnalysisTask, TaskEvent
from app.tasks.repository import TaskRepository
from app.workspace.models import Message, WorkspaceSession


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


def _finish(conclusion: str = "") -> AgentDecision:
    return AgentDecision(action="finish", conclusion=conclusion)


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
        self.save_plan_result = True
        self.save_trajectory_result = True
        self.terminal_results = {
            "completed": True,
            "completed_with_warnings": True,
            "cancelled": True,
            "interrupted": True,
            "failed": True,
            "insufficient_balance": True,
        }

    async def claim_lease(self, task_id, worker_id, lease_seconds):
        return self.task

    async def save_plan(self, task_id, worker_id, plan_json):
        if not self.save_plan_result:
            return False
        self.task.plan_json = plan_json
        self.trajectories.append(plan_json)
        return True

    async def save_trajectory(self, task_id, worker_id, trajectory_json):
        if not self.save_trajectory_result:
            return False
        self.task.plan_json = trajectory_json
        self.trajectories.append(trajectory_json)
        return True

    async def cancel_requested(self, task_id):
        return False

    async def renew_lease(self, task_id, worker_id, lease_seconds):
        return True

    async def mark_completed(self, task_id, worker_id):
        if not self.terminal_results["completed"]:
            return False
        self.terminal = "completed"
        return True

    async def mark_completed_with_warnings(self, task_id, worker_id, code, message=None):
        if not self.terminal_results["completed_with_warnings"]:
            return False
        self.terminal = f"completed_with_warnings:{code}"
        return True

    async def mark_cancelled(self, task_id, worker_id):
        if not self.terminal_results["cancelled"]:
            return False
        self.terminal = "cancelled"
        return True

    async def mark_interrupted(self, task_id, worker_id):
        if not self.terminal_results["interrupted"]:
            return False
        self.terminal = "interrupted"
        return True

    async def mark_failed(self, task_id, worker_id, code, message=None):
        if not self.terminal_results["failed"]:
            return False
        self.terminal = f"failed:{code}"
        return True

    async def mark_insufficient_balance(self, task_id, worker_id):
        if not self.terminal_results["insufficient_balance"]:
            return False
        self.terminal = "insufficient_balance"
        return True

    async def append_event(self, task_id, user_id, event_type, payload):
        self.events.append((str(event_type), payload))

    async def release_lease(self, task_id, worker_id):
        return None


class _FakeContextBuilder:
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
        self.contexts: list[AgentLoopContext] = []

    async def agent_decide(self, context):
        self.calls += 1
        self.contexts.append(context)
        return self._decisions.pop(0)


class _FakeGateway:
    def __init__(self, rows: list) -> None:
        self._rows = list(rows)
        self.commands: list[tuple] = []

    async def execute_batch(self, commands):
        self.commands.append(commands)
        item = self._rows.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeArtifacts:
    def __init__(self) -> None:
        self.conclusions: list[tuple[str, str]] = []
        self.calls: list[str] = []

    async def write_conclusion_message(self, task_id, conclusion):
        self.calls.append("conclusion")
        self.conclusions.append((task_id, conclusion))

    async def auto_kol_analysis(self, task_id):
        self.calls.append("auto_analysis")


class FakeGoalPlannerShadow:
    def __init__(
        self,
        error: Exception | None = None,
        *,
        store: _FakeStore | None = None,
    ) -> None:
        self.task_ids: list[str] = []
        self.terminals_at_call: list[str | None] = []
        self.error = error
        self.store = store

    async def plan_task(self, task_id: str) -> None:
        self.task_ids.append(task_id)
        self.terminals_at_call.append(self.store.terminal if self.store is not None else None)
        if self.error is not None:
            raise self.error


def _task(
    plan_json: dict | None = None,
    *,
    retry_of_task_id: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id="task-1",
        user_id="user-1",
        session_id="session-1",
        kind="agent",
        plan_json=plan_json,
        retry_of_task_id=retry_of_task_id,
    )


def _executor(
    store,
    decider,
    gateway,
    artifacts,
    context_builder=None,
    *,
    goal_planner_shadow=None,
) -> TaskExecutor:
    return TaskExecutor(
        repository=store,
        context_builder=context_builder or _FakeContextBuilder(),
        planner=decider,
        gateway=gateway,
        artifacts=artifacts,
        goal_planner_shadow=goal_planner_shadow,
        worker_id="worker-1",
        lease_seconds=60,
        heartbeat_seconds=0.05,
    )


@pytest.mark.asyncio
async def test_shadow_goal_planner_runs_after_legacy_agent_loop() -> None:
    task = _task()
    store = _FakeStore(task)
    shadow = FakeGoalPlannerShadow(store=store)
    decider = _ScriptedDecider([_call(), _finish("旧流程正常完成")])
    executor = _executor(
        store,
        decider,
        _FakeGateway([(_settled(),)]),
        _FakeArtifacts(),
        goal_planner_shadow=shadow,
    )

    await executor.run(task.id)

    assert shadow.task_ids == ["task-1"]
    assert shadow.terminals_at_call == ["completed"]
    assert decider.calls == 2
    assert store.terminal == "completed"


@pytest.mark.parametrize(
    ("failed_write", "decisions", "expected_decider_calls"),
    [
        ("save_plan_result", [_finish("不会执行")], 0),
        ("save_trajectory_result", [_call()], 1),
    ],
)
@pytest.mark.asyncio
async def test_shadow_goal_planner_skips_when_lease_write_fails(
    failed_write: str,
    decisions: list[AgentDecision],
    expected_decider_calls: int,
) -> None:
    task = _task()
    store = _FakeStore(task)
    setattr(store, failed_write, False)
    shadow = FakeGoalPlannerShadow(store=store)
    decider = _ScriptedDecider(decisions)
    executor = _executor(
        store,
        decider,
        _FakeGateway([]),
        _FakeArtifacts(),
        goal_planner_shadow=shadow,
    )

    await executor.run(task.id)

    assert shadow.task_ids == []
    assert decider.calls == expected_decider_calls
    assert store.terminal is None


@pytest.mark.asyncio
async def test_shadow_goal_planner_skips_when_completed_terminal_persistence_fails() -> None:
    task = _task()
    store = _FakeStore(task)
    store.terminal_results["completed"] = False
    shadow = FakeGoalPlannerShadow(store=store)
    decider = _ScriptedDecider([_call(), _finish("旧流程尝试完成")])
    executor = _executor(
        store,
        decider,
        _FakeGateway([(_settled(),)]),
        _FakeArtifacts(),
        goal_planner_shadow=shadow,
    )

    await executor.run(task.id)

    assert len(store.trajectories[-1]["results"]) == 1
    assert store.terminal is None
    assert shadow.task_ids == []


@pytest.mark.asyncio
async def test_shadow_goal_planner_failure_does_not_fail_task(
    caplog: pytest.LogCaptureFixture,
) -> None:
    task = _task()
    store = _FakeStore(task)
    sensitive_marker = "sensitive-model-output-must-not-leak"
    shadow = FakeGoalPlannerShadow(RuntimeError(sensitive_marker))
    decider = _ScriptedDecider([_call(), _finish("旧流程正常完成")])
    executor = _executor(
        store,
        decider,
        _FakeGateway([(_settled(),)]),
        _FakeArtifacts(),
        goal_planner_shadow=shadow,
    )

    with caplog.at_level("WARNING", logger="app.tasks.executor"):
        await executor.run(task.id)

    assert shadow.task_ids == ["task-1"]
    assert decider.calls == 2
    assert store.terminal == "completed"
    record = next(
        item for item in caplog.records if item.getMessage().startswith("goal_planner_shadow_failed")
    )
    assert record.getMessage() == "goal_planner_shadow_failed task_id=task-1"
    assert record.exc_info is None
    assert sensitive_marker not in caplog.text


@pytest.mark.asyncio
async def test_shadow_goal_planner_skips_retry_task() -> None:
    task = _task(retry_of_task_id="source-task")
    store = _FakeStore(task)
    shadow = FakeGoalPlannerShadow()
    decider = _ScriptedDecider([_call(), _finish("重试旧流程正常完成")])
    executor = _executor(
        store,
        decider,
        _FakeGateway([(_settled(),)]),
        _FakeArtifacts(),
        goal_planner_shadow=shadow,
    )

    await executor.run(task.id)

    assert shadow.task_ids == []
    assert decider.calls == 2
    assert store.terminal == "completed"


@pytest.mark.asyncio
async def test_agent_loop_calls_tools_until_finish_then_writes_conclusion() -> None:
    task = _task()
    store = _FakeStore(task)
    decider = _ScriptedDecider([_call(), _call(), _finish("已圈选 12 位达人，覆盖小红书与抖音。")])
    gateway = _FakeGateway([(_settled(),), (_settled(),)])
    artifacts = _FakeArtifacts()

    await _executor(store, decider, gateway, artifacts).run(task.id)

    assert store.terminal == "completed"
    # finish 的结论原样写入 assistant 消息，不再生成分析报告与流式摘要。
    assert artifacts.conclusions == [(task.id, "已圈选 12 位达人，覆盖小红书与抖音。")]
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
async def test_agent_loop_insufficient_balance_with_evidence_writes_conclusion() -> None:
    task = _task()
    store = _FakeStore(task)
    decider = _ScriptedDecider([_call(), _call()])
    gateway = _FakeGateway([(_settled(),), InsufficientPointsError()])
    artifacts = _FakeArtifacts()

    await _executor(store, decider, gateway, artifacts).run(task.id)

    # 已有 settled 证据：仍写结论消息（空结论走服务端回退文案），再进入余额不足终态。
    assert store.terminal == "insufficient_balance"
    assert artifacts.conclusions == [(task.id, "")]
    assert len(gateway.commands) == 2


@pytest.mark.asyncio
async def test_agent_loop_insufficient_balance_without_evidence_has_no_message() -> None:
    task = _task()
    store = _FakeStore(task)
    decider = _ScriptedDecider([_call()])
    gateway = _FakeGateway([InsufficientPointsError()])
    artifacts = _FakeArtifacts()

    await _executor(store, decider, gateway, artifacts).run(task.id)

    assert store.terminal == "insufficient_balance"
    assert artifacts.conclusions == []
    assert artifacts.calls == []


@pytest.mark.asyncio
async def test_agent_loop_completed_runs_auto_kol_analysis_after_conclusion() -> None:
    task = _task()
    store = _FakeStore(task)
    decider = _ScriptedDecider([_call(), _finish("已圈选 3 位达人。")])
    gateway = _FakeGateway([(_settled(),)])
    artifacts = _FakeArtifacts()

    await _executor(store, decider, gateway, artifacts).run(task.id)

    assert store.terminal == "completed"
    # 顺序契约：先结论消息、后自动分析，保证 report.updated 先于终态事件发出。
    assert artifacts.calls == ["conclusion", "auto_analysis"]


@pytest.mark.asyncio
async def test_agent_loop_completed_with_warnings_runs_auto_kol_analysis_after_conclusion() -> None:
    task = _task()
    store = _FakeStore(task)
    decider = _ScriptedDecider([_call(), _call(), _finish()])
    gateway = _FakeGateway([(_released(),), (_settled(),)])
    artifacts = _FakeArtifacts()

    await _executor(store, decider, gateway, artifacts).run(task.id)

    assert store.terminal == "completed_with_warnings:mcp_partial_failure"
    assert artifacts.calls == ["conclusion", "auto_analysis"]


@pytest.mark.asyncio
async def test_agent_loop_insufficient_balance_with_evidence_runs_auto_kol_analysis() -> None:
    task = _task()
    store = _FakeStore(task)
    decider = _ScriptedDecider([_call(), _call()])
    gateway = _FakeGateway([(_settled(),), InsufficientPointsError()])
    artifacts = _FakeArtifacts()

    await _executor(store, decider, gateway, artifacts).run(task.id)

    assert store.terminal == "insufficient_balance"
    assert artifacts.calls == ["conclusion", "auto_analysis"]


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
    assert artifacts.conclusions == []


@pytest.mark.asyncio
async def test_agent_loop_without_any_evidence_fails() -> None:
    task = _task()
    store = _FakeStore(task)
    decider = _ScriptedDecider([_call(), _call(), _finish()])
    gateway = _FakeGateway([(_released(),), (_released(),)])
    artifacts = _FakeArtifacts()

    await _executor(store, decider, gateway, artifacts).run(task.id)

    assert store.terminal == "failed:no_evidence_collected"
    assert artifacts.conclusions == []


@pytest.mark.asyncio
async def test_agent_loop_first_round_finish_without_evidence_fails() -> None:
    # 门禁拆除后模型首轮即可 finish：零证据直接失败，语义化错误码，不写结论消息。
    task = _task()
    store = _FakeStore(task)
    decider = _ScriptedDecider([_finish("没有数据也要结论")])
    gateway = _FakeGateway([])
    artifacts = _FakeArtifacts()

    await _executor(store, decider, gateway, artifacts).run(task.id)

    assert store.terminal == "failed:no_evidence_collected"
    assert gateway.commands == []
    assert artifacts.conclusions == []


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


@pytest.mark.asyncio
async def test_agent_loop_throttles_same_tool_after_repeated_empty_results() -> None:
    task = _task()
    store = _FakeStore(task)
    # 前 2 次空结果正常执行；第 3 次同名调用被熔断（不发起调用、不扣费），
    # 模型随后 finish，任务正常完成。
    decider = _ScriptedDecider([_call(), _call(), _call(), _finish()])
    gateway = _FakeGateway([(_settled(data={}),), (_settled(data={}),)])
    artifacts = _FakeArtifacts()

    await _executor(store, decider, gateway, artifacts).run(task.id)

    assert store.terminal == "completed"
    assert len(gateway.commands) == 2
    assert decider.calls == 4
    # 熔断原因回喂进下一轮上下文，且不占 invalid_streak。
    throttle_note = next(
        note for note in decider.contexts[-1].notes if note.step_id == "throttle_1"
    )
    assert "返回空数据" in throttle_note.summary
    assert _TOOL_NAME in throttle_note.summary


@pytest.mark.asyncio
async def test_agent_loop_throttle_streak_breaks_loop_with_existing_evidence() -> None:
    task = _task()
    store = _FakeStore(task)
    # 模型执迷于已被熔断的工具：连续 3 次熔断后按现有证据收尾，防止零成本死循环。
    decider = _ScriptedDecider([_call(), _call(), _call(), _call(), _call()])
    gateway = _FakeGateway([(_settled(data={}),), (_settled(data={}),)])
    artifacts = _FakeArtifacts()

    await _executor(store, decider, gateway, artifacts).run(task.id)

    assert store.terminal == "completed"
    assert len(gateway.commands) == 2
    # 2 次执行 + 3 次被熔断（第 3 次熔断后直接收尾，不再询问模型）。
    assert decider.calls == 5
    assert artifacts.conclusions == [(task.id, "")]
    # 前 2 次熔断原因已回喂进后续轮次上下文。
    throttle_ids = {
        note.step_id
        for context in decider.contexts
        for note in context.notes
        if note.step_id.startswith("throttle_")
    }
    assert throttle_ids == {"throttle_1", "throttle_2"}


@pytest.mark.asyncio
async def test_agent_loop_throttle_does_not_block_other_tools() -> None:
    task = _task()
    store = _FakeStore(task)
    stat_call = AgentDecision(
        action="call_tool",
        internal_tool_name=_STAT_TOOL_NAME,
        arguments={
            "name": "格力",
            "datasource": ["douyin"],
            "start_time": "2026-06-01",
            "end_time": "2026-07-01",
            "target_type": "keyword",
        },
        evidence_goal="声量",
    )
    decider = _ScriptedDecider([_call(), _call(), _call(), stat_call, _finish()])
    gateway = _FakeGateway([
        (_settled(data={}),),
        (_settled(data={}),),
        (_settled(),),
    ])
    artifacts = _FakeArtifacts()

    await _executor(store, decider, gateway, artifacts).run(task.id)

    assert store.terminal == "completed"
    # 熔断只针对累计空结果的同名工具，其他工具不受影响：
    # 2 次空结果执行 + 第 3 次同名调用被熔断 + 1 次统计工具执行。
    assert len(gateway.commands) == 3


class _FakeSelectionIngest:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict] = []

    async def ingest(self, **kwargs):
        if self.fail:
            raise RuntimeError("selection_store_down")
        self.calls.append(kwargs)


def _executor_with_selection(store, decider, gateway, artifacts, selection) -> TaskExecutor:
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
async def test_agent_loop_ingests_settled_evidence_into_selection() -> None:
    task = _task()
    store = _FakeStore(task)
    decider = _ScriptedDecider([_call(), _finish()])
    structured = {"result": "{}"}
    gateway = _FakeGateway([(_settled(data=structured),)])
    artifacts = _FakeArtifacts()
    selection = _FakeSelectionIngest()

    await _executor_with_selection(store, decider, gateway, artifacts, selection).run(task.id)

    assert store.terminal == "completed"
    assert selection.calls == [
        {
            "user_id": "user-1",
            "session_id": "session-1",
            "task_id": "task-1",
            "internal_tool_name": _TOOL_NAME,
            "structured_content": structured,
            # 调用参数必须透传给沉淀钩子：kol.detail/insight 工具的平台
            # 身份靠 arguments 里的 platform/datasource 注入。
            "arguments": {"keyword": "美妆"},
        }
    ]


@pytest.mark.asyncio
async def test_agent_loop_selection_ingest_failure_does_not_block() -> None:
    task = _task()
    store = _FakeStore(task)
    decider = _ScriptedDecider([_call(), _finish()])
    gateway = _FakeGateway([(_settled(),)])
    artifacts = _FakeArtifacts()
    selection = _FakeSelectionIngest(fail=True)

    await _executor_with_selection(store, decider, gateway, artifacts, selection).run(task.id)

    # 沉淀失败只记 warning：任务仍正常完成并写结论消息。
    assert store.terminal == "completed"
    assert artifacts.conclusions == [(task.id, "")]


async def _create_leased_task(worker_id: str) -> dict[str, str]:
    """在测试库写入持有有效租约的任务及其外键链，返回各实体 id。"""
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
                id=ids["user_id"],
                nickname="结论消息测试",
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
                title="结论消息测试",
                brand="测试品牌",
                status="draft",
                platforms=["xiaohongshu"],
                target_audience="测试受众",
                last_accessed_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        # 模型间无 ORM relationship，需逐级显式 flush 保证外键父行先落库
        #（messages.session_id → sessions、analysis_tasks.trigger_message_id → messages）。
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
                lease_owner=worker_id,
                lease_expires_at=now + timedelta(minutes=5),
                started_at=now,
                created_at=now,
                updated_at=now,
            )
        )
    return ids


async def _cleanup_leased_task(ids: dict[str, str]) -> None:
    async with SessionFactory.begin() as db:
        await db.execute(delete(TaskEvent).where(TaskEvent.task_id == ids["task_id"]))
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


@pytest.mark.parametrize("lease_is_valid", [True, False])
@pytest.mark.parametrize(
    ("method_name", "extra_args", "expected_status"),
    [
        ("mark_completed", (), "completed"),
        (
            "mark_completed_with_warnings",
            ("mcp_partial_failure", "部分查询失败"),
            "completed_with_warnings",
        ),
        ("mark_cancelled", (), "cancelled"),
        ("mark_interrupted", (), "interrupted"),
        ("mark_failed", ("upstream_error", None), "failed"),
        ("mark_insufficient_balance", (), "insufficient_balance"),
    ],
)
@pytest.mark.asyncio
async def test_task_repository_terminal_methods_report_persistence(
    method_name: str,
    extra_args: tuple[object, ...],
    expected_status: str,
    lease_is_valid: bool,
) -> None:
    worker_id = f"test-worker-{uuid4()}"
    ids = await _create_leased_task(worker_id)
    try:
        if not lease_is_valid:
            async with SessionFactory.begin() as db:
                task = await db.get(AnalysisTask, ids["task_id"])
                assert task is not None
                task.lease_expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1)

        async with SessionFactory.begin() as db:
            repository = TaskRepository(db)
            result = await getattr(repository, method_name)(
                ids["task_id"],
                worker_id,
                *extra_args,
            )
            task = await db.get(AnalysisTask, ids["task_id"])
            assert task is not None
            assert result is lease_is_valid
            assert task.status == (expected_status if lease_is_valid else "running")
    finally:
        await _cleanup_leased_task(ids)


@pytest.mark.asyncio
async def test_write_conclusion_message_persists_assistant_message_idempotently() -> None:
    worker_id = f"test-worker-{uuid4()}"
    ids = await _create_leased_task(worker_id)
    try:
        artifacts = _TaskArtifacts(worker_id, model=None)
        await artifacts.write_conclusion_message(ids["task_id"], "已圈选 12 位达人，覆盖小红书。")
        # 重试安全：相同 task_id 的结论消息已存在时直接返回，不重复写入。
        await artifacts.write_conclusion_message(ids["task_id"], "重试不应重复写入")

        async with SessionFactory() as db:
            messages = list(
                (
                    await db.scalars(
                        select(Message).where(
                            Message.session_id == ids["session_id"],
                            Message.role == "assistant",
                        )
                    )
                ).all()
            )
            assert len(messages) == 1
            message = messages[0]
            assert message.content == "已圈选 12 位达人，覆盖小红书。"
            assert message.metadata_json == {
                "task_id": ids["task_id"],
                "kind": "conclusion",
                "status": "completed",
            }
            event = await db.scalar(
                select(TaskEvent).where(
                    TaskEvent.task_id == ids["task_id"],
                    TaskEvent.event_type == "message.completed",
                )
            )
            assert event is not None
            # 事件契约与前端 reducer（taskEvents.ts message.completed 读
            # payload.text 渲染气泡）耦合：结论全文必须随事件下发。
            assert event.payload_json == {
                "message_id": message.id,
                "text": "已圈选 12 位达人，覆盖小红书。",
            }
    finally:
        await _cleanup_leased_task(ids)


@pytest.mark.asyncio
async def test_write_conclusion_message_empty_conclusion_falls_back_to_selection_count() -> None:
    worker_id = f"test-worker-{uuid4()}"
    ids = await _create_leased_task(worker_id)
    try:
        now = datetime.now(UTC).replace(tzinfo=None)
        async with SessionFactory.begin() as db:
            for index in range(3):
                db.add(
                    SessionKolSelection(
                        id=str(uuid4()),
                        user_id=ids["user_id"],
                        session_id=ids["session_id"],
                        platform="xiaohongshu",
                        kol_uid=f"uid-{index}",
                        fields_json={},
                        score_json={},
                        created_at=now,
                        updated_at=now,
                    )
                )
        artifacts = _TaskArtifacts(worker_id, model=None)
        await artifacts.write_conclusion_message(ids["task_id"], "")

        async with SessionFactory() as db:
            message = await db.scalar(
                select(Message).where(
                    Message.session_id == ids["session_id"],
                    Message.role == "assistant",
                )
            )
            assert message is not None
            assert "共圈选 3 位达人" in message.content
            assert message.metadata_json["kind"] == "conclusion"
            # 回退文案同样必须随 message.completed 事件下发（payload.text）。
            event = await db.scalar(
                select(TaskEvent).where(
                    TaskEvent.task_id == ids["task_id"],
                    TaskEvent.event_type == "message.completed",
                )
            )
            assert event is not None
            assert event.payload_json["text"] == message.content
    finally:
        await _cleanup_leased_task(ids)


def _analysis_document() -> ReportDocument:
    return ReportDocument(
        title="KOL 圈选分析",
        conclusion="名单质量良好。",
        blocks=[MetricGridBlock(items=[MetricItem(label="圈选总数", value=2)])],
    )


class _FakeAnalysisModel:
    """kol_analysis 专用模型 stub：document 为 None 时模拟模型输出校验失败。"""

    def __init__(self, document: ReportDocument | None) -> None:
        self.document = document

    async def complete_json(self, request):
        if self.document is None:
            raise ModelPlanInvalidError("MODEL_PLAN_INVALID", retryable=False)
        return StructuredResult(
            value=self.document,
            usage=None,
            request_id="req-test",
            regeneration_count=0,
        )


async def _seed_selection_rows(ids: dict[str, str], count: int) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    async with SessionFactory.begin() as db:
        for index in range(count):
            db.add(
                SessionKolSelection(
                    id=str(uuid4()),
                    user_id=ids["user_id"],
                    session_id=ids["session_id"],
                    platform="xiaohongshu",
                    kol_uid=f"uid-{index}",
                    nickname=f"达人{index}",
                    fields_json={},
                    score_json={"total": 80.0, "rating": "重点推荐"},
                    created_at=now,
                    updated_at=now,
                )
            )


@pytest.mark.asyncio
async def test_auto_kol_analysis_builds_session_report_and_emits_event() -> None:
    worker_id = f"test-worker-{uuid4()}"
    ids = await _create_leased_task(worker_id)
    try:
        await _seed_selection_rows(ids, 2)
        artifacts = _TaskArtifacts(worker_id, model=_FakeAnalysisModel(_analysis_document()))

        await artifacts.auto_kol_analysis(ids["task_id"])

        async with SessionFactory() as db:
            report = await db.scalar(
                select(AnalysisReport).where(
                    AnalysisReport.session_id == ids["session_id"]
                )
            )
            assert report is not None
            # 自动分析与手动 kol-analysis 同契约：会话级报告（task_id 为 NULL）。
            assert report.task_id is None
            assert report.version == 1
            assert report.title == "KOL 圈选分析"
            event = await db.scalar(
                select(TaskEvent).where(
                    TaskEvent.task_id == ids["task_id"],
                    TaskEvent.event_type == "report.updated",
                )
            )
            assert event is not None
            # payload 格式与任务级 build() 一致，前端 reducer 按 report_id 拉取展示。
            assert event.payload_json == {
                "report_id": report.id,
                "version": 1,
                "phase": "ai_summary",
                "label": "KOL 分析报告已生成",
            }
    finally:
        await _cleanup_leased_task(ids)


@pytest.mark.asyncio
async def test_auto_kol_analysis_skips_empty_selection() -> None:
    worker_id = f"test-worker-{uuid4()}"
    ids = await _create_leased_task(worker_id)
    try:
        artifacts = _TaskArtifacts(worker_id, model=_FakeAnalysisModel(_analysis_document()))

        await artifacts.auto_kol_analysis(ids["task_id"])

        async with SessionFactory() as db:
            assert (
                await db.scalar(
                    select(AnalysisReport).where(
                        AnalysisReport.session_id == ids["session_id"]
                    )
                )
                is None
            )
            assert (
                await db.scalar(
                    select(TaskEvent).where(
                        TaskEvent.task_id == ids["task_id"],
                        TaskEvent.event_type == "report.updated",
                    )
                )
                is None
            )
    finally:
        await _cleanup_leased_task(ids)


@pytest.mark.asyncio
async def test_auto_kol_analysis_without_model_is_noop() -> None:
    worker_id = f"test-worker-{uuid4()}"
    ids = await _create_leased_task(worker_id)
    try:
        await _seed_selection_rows(ids, 2)
        artifacts = _TaskArtifacts(worker_id, model=None)

        await artifacts.auto_kol_analysis(ids["task_id"])

        async with SessionFactory() as db:
            assert (
                await db.scalar(
                    select(AnalysisReport).where(
                        AnalysisReport.session_id == ids["session_id"]
                    )
                )
                is None
            )
    finally:
        await _cleanup_leased_task(ids)


@pytest.mark.asyncio
async def test_auto_kol_analysis_replay_is_idempotent() -> None:
    worker_id = f"test-worker-{uuid4()}"
    ids = await _create_leased_task(worker_id)
    try:
        await _seed_selection_rows(ids, 2)
        artifacts = _TaskArtifacts(worker_id, model=_FakeAnalysisModel(_analysis_document()))

        await artifacts.auto_kol_analysis(ids["task_id"])
        # 崩溃恢复重放收尾段：已发过 report.updated 的任务不得重复生成报告与事件。
        await artifacts.auto_kol_analysis(ids["task_id"])

        async with SessionFactory() as db:
            reports = list(
                (
                    await db.scalars(
                        select(AnalysisReport).where(
                            AnalysisReport.session_id == ids["session_id"]
                        )
                    )
                ).all()
            )
            assert [report.version for report in reports] == [1]
            events = list(
                (
                    await db.scalars(
                        select(TaskEvent).where(
                            TaskEvent.task_id == ids["task_id"],
                            TaskEvent.event_type == "report.updated",
                        )
                    )
                ).all()
            )
            assert len(events) == 1
    finally:
        await _cleanup_leased_task(ids)


@pytest.mark.asyncio
async def test_auto_kol_analysis_model_error_does_not_propagate() -> None:
    worker_id = f"test-worker-{uuid4()}"
    ids = await _create_leased_task(worker_id)
    try:
        await _seed_selection_rows(ids, 2)
        artifacts = _TaskArtifacts(worker_id, model=_FakeAnalysisModel(None))

        # 模型错误只记 warning，绝不向执行器传播阻塞任务收尾。
        await artifacts.auto_kol_analysis(ids["task_id"])

        async with SessionFactory() as db:
            assert (
                await db.scalar(
                    select(AnalysisReport).where(
                        AnalysisReport.session_id == ids["session_id"]
                    )
                )
                is None
            )
            assert (
                await db.scalar(
                    select(TaskEvent).where(
                        TaskEvent.task_id == ids["task_id"],
                        TaskEvent.event_type == "report.updated",
                    )
                )
                is None
            )
    finally:
        await _cleanup_leased_task(ids)
