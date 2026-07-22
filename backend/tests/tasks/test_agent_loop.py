from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from sqlalchemy import delete, select

from app.billing.service import InsufficientPointsError
from app.db.session import SessionFactory
from app.identity.models import User
from app.mcp_gateway.contracts import DataTapService
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

    async def mark_insufficient_balance(self, task_id, worker_id):
        self.terminal = "insufficient_balance"

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

    async def write_conclusion_message(self, task_id, conclusion):
        self.conclusions.append((task_id, conclusion))


def _task(plan_json: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id="task-1",
        user_id="user-1",
        session_id="session-1",
        kind="agent",
        plan_json=plan_json,
    )


def _executor(store, decider, gateway, artifacts, context_builder=None) -> TaskExecutor:
    return TaskExecutor(
        repository=store,
        context_builder=context_builder or _FakeContextBuilder(),
        planner=decider,
        gateway=gateway,
        artifacts=artifacts,
        worker_id="worker-1",
        lease_seconds=60,
        heartbeat_seconds=0.05,
    )


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
            delete(SessionKolSelection).where(
                SessionKolSelection.session_id == ids["session_id"]
            )
        )
        await db.execute(delete(Message).where(Message.session_id == ids["session_id"]))
        await db.execute(delete(WorkspaceSession).where(WorkspaceSession.id == ids["session_id"]))
        await db.execute(delete(User).where(User.id == ids["user_id"]))


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
            assert event.payload_json == {"message_id": message.id}
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
    finally:
        await _cleanup_leased_task(ids)
