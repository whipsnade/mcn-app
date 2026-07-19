import json
from types import SimpleNamespace

import pytest

from app.orchestration.schemas import ToolPlan, ToolPlanStep
from app.tasks.executor import TaskExecutor


_SEARCH_TOOL = "datatap.douyin.kol.search.v1"
_DETAIL_TOOL = "datatap.social.grow.kol.detail.v1"


def _search_payload(uids: list[str]) -> dict:
    rows = [{"账号ID": uid, "昵称": f"达人{index}"} for index, uid in enumerate(uids, start=1)]
    return {"result": json.dumps({"KOL 列表": rows}, ensure_ascii=False)}


def _settled(payload: dict | None, step_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        status="settled",
        internal_tool_name=_SEARCH_TOOL,
        plan_step_id=step_id,
        evidence_json={"structured_content": payload},
        error_type=None,
    )


def _plan() -> ToolPlan:
    return ToolPlan(
        objective="达人分析",
        steps=(
            ToolPlanStep(
                id="step_1",
                internal_tool_name=_SEARCH_TOOL,
                arguments={"request": {"page": 1, "size": 10}},
                evidence_kind="kol",
                evidence_goal="搜索候选",
            ),
            ToolPlanStep(
                id="step_2",
                internal_tool_name=_DETAIL_TOOL,
                arguments={"kwUidList": [], "platform": "douyin"},
                depends_on=("step_1",),
                evidence_kind="kol",
                evidence_goal="详情补全",
            ),
        ),
    )


class _FakeStore:
    def __init__(self, task: SimpleNamespace) -> None:
        self.task = task
        self.events: list[tuple[str, dict]] = []
        self.plan_revisions: list[tuple[dict | None, dict | None]] = []
        self.terminal: str | None = None

    async def claim_lease(self, task_id, worker_id, lease_seconds):
        return self.task

    async def save_plan(self, task_id, worker_id, plan_json):
        return True

    async def save_trajectory(self, task_id, worker_id, trajectory_json):
        return True

    async def save_plan_revision(self, task_id, worker_id, plan_json=None, replan_json=None):
        self.plan_revisions.append((plan_json, replan_json))
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
    async def build(self, user_id, session_id):
        return object()


class _FixedPlanner:
    def __init__(self, plan: ToolPlan) -> None:
        self._plan = plan

    async def plan(self, context):
        return self._plan


class _FakeGateway:
    def __init__(self, batches: list[tuple]) -> None:
        self._batches = list(batches)
        self.commands: list[tuple] = []

    async def execute_batch(self, commands):
        self.commands.append(commands)
        return self._batches.pop(0)


class _FakeArtifacts:
    async def build_candidates(self, task_id):
        return None

    async def build_bi_report(self, task_id):
        return SimpleNamespace(chart_data_json={})

    async def stream_summary(self, task_id):
        return None


def _task() -> SimpleNamespace:
    return SimpleNamespace(
        id="task-1",
        user_id="user-1",
        session_id="session-1",
        kind="pipeline",
        plan_json=None,
        replan_json=None,
        max_calls=10,
    )


def _executor(store, gateway) -> TaskExecutor:
    return TaskExecutor(
        repository=store,
        context_builder=_FakeContextBuilder(),
        planner=_FixedPlanner(_plan()),
        gateway=gateway,
        artifacts=_FakeArtifacts(),
        worker_id="worker-1",
        lease_seconds=60,
        heartbeat_seconds=0.05,
    )


@pytest.mark.asyncio
async def test_detail_step_is_backfilled_with_search_uids() -> None:
    task = _task()
    store = _FakeStore(task)
    gateway = _FakeGateway([
        (_settled(_search_payload(["uid-1", "uid-2"]), "step_1"),),
        (_settled(None, "step_2"),),
    ])

    await _executor(store, gateway).run(task.id)

    assert store.terminal == "completed"
    assert len(gateway.commands) == 2
    detail_command = gateway.commands[1][0]
    assert detail_command.plan_step_id == "step_2"
    assert detail_command.arguments["kwUidList"] == ["uid-1", "uid-2"]
    # 空 scope 归一化为平台有效的默认集合。
    assert detail_command.arguments["scope"] == ["accountTrend", "fansAudience", "postSummaryStatistics"]
    # 回填后的计划已静默持久化（网关 digest 复核依赖它）。
    assert store.plan_revisions
    revised = store.plan_revisions[0][0]
    detail_step = next(s for s in revised["steps"] if s["id"] == "step_2")
    assert detail_step["arguments"]["kwUidList"] == ["uid-1", "uid-2"]


@pytest.mark.asyncio
async def test_detail_step_is_skipped_without_charge_when_search_is_empty() -> None:
    task = _task()
    store = _FakeStore(task)
    gateway = _FakeGateway([
        (_settled(_search_payload([]), "step_1"),),
    ])

    await _executor(store, gateway).run(task.id)

    assert store.terminal == "completed"
    # 详情调用被跳过：只有一次网关调用，不发出 tool.started，不写计划修订。
    assert len(gateway.commands) == 1
    assert store.plan_revisions == []
    started = [payload for event, payload in store.events if event == "tool.started"]
    assert all(_DETAIL_TOOL not in str(payload) for payload in started)


def _tag_then_search_plan() -> ToolPlan:
    return ToolPlan(
        objective="餐饮达人分析",
        steps=(
            ToolPlanStep(
                id="step_1",
                internal_tool_name="datatap.insight.match.best.tag.v1",
                arguments={"tag_type": "品类标签", "tag_names": ["餐饮"]},
                evidence_kind="kol",
                evidence_goal="标签匹配",
            ),
            ToolPlanStep(
                id="step_2",
                internal_tool_name=_SEARCH_TOOL,
                arguments={"request": {"page": 1, "size": 10}},
                depends_on=("step_1",),
                evidence_kind="kol",
                evidence_goal="搜索候选",
            ),
        ),
    )


@pytest.mark.asyncio
async def test_retryable_batch_failure_no_longer_aborts_following_steps() -> None:
    """标签匹配等辅助步骤失败（可重试）时，后续批次继续执行。"""
    task = _task()
    store = _FakeStore(task)
    released = SimpleNamespace(
        status="released",
        internal_tool_name="datatap.insight.match.best.tag.v1",
        plan_step_id="step_1",
        evidence_json={"outcome": "failed"},
        error_type="upstream_timeout",
    )
    gateway = _FakeGateway([
        (released,),
        (_settled(_search_payload(["uid-1"]), "step_2"),),
    ])
    executor = TaskExecutor(
        repository=store,
        context_builder=_FakeContextBuilder(),
        planner=_FixedPlanner(_tag_then_search_plan()),
        gateway=gateway,
        artifacts=_FakeArtifacts(),
        worker_id="worker-1",
        lease_seconds=60,
        heartbeat_seconds=0.05,
    )

    await executor.run(task.id)

    # 两批都执行了；标签失败计入降级而不是整任务失败。
    assert len(gateway.commands) == 2
    assert store.terminal == "completed_with_warnings:mcp_partial_failure"


@pytest.mark.asyncio
async def test_detail_scope_is_normalized_to_platform_vocabulary() -> None:
    """模型自造的 scope 取值被归一化，有效的取值保留。"""
    task = _task()
    store = _FakeStore(task)
    gateway = _FakeGateway([
        (_settled(_search_payload(["uid-1"]), "step_1"),),
        (_settled(None, "step_2"),),
    ])
    plan = _plan()
    crazy_scope = {"kwUidList": [], "platform": "douyin", "scope": ["followers", "fansAudience"]}
    plan = plan.model_copy(update={"steps": (
        plan.steps[0],
        plan.steps[1].model_copy(update={"arguments": crazy_scope}),
    )})
    executor = TaskExecutor(
        repository=store,
        context_builder=_FakeContextBuilder(),
        planner=_FixedPlanner(plan),
        gateway=gateway,
        artifacts=_FakeArtifacts(),
        worker_id="worker-1",
        lease_seconds=60,
        heartbeat_seconds=0.05,
    )

    await executor.run(task.id)

    detail_command = gateway.commands[1][0]
    # 自造的 followers 被剔除，平台有效的 fansAudience 保留。
    assert detail_command.arguments["scope"] == ["fansAudience"]
