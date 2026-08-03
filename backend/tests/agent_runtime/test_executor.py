"""AgentRunExecutor 集成测试（Task 15 / 设计文档 §七 / §八）。

覆盖：
1. queued Run 被 worker 领取、经 Task 14 引擎执行并到达终态；
2. 过期租约接管：新 worker 领取、新建 Attempt（attempt 递增）、从最后完整
   Step 继续（完整 Step 不重放，sequence 不归零）；
3. 优雅关闭：停止领取新 Run，等待当前 Run 在事务安全点完成。
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.agent_runtime.engine import AgentEngine
from app.agent_runtime.events import AgentEventBroker, AgentEventStream
from app.agent_runtime.executor import AgentRunExecutor
from app.agent_runtime.models import (
    AgentEvent,
    AgentMessage,
    AgentRun,
    AgentRunAttempt,
    AgentSession,
    AgentStep,
)
from app.agent_runtime.repository import AgentRunRepository, utc_now
from app.agent_runtime.reviewer import ReviewerDriver
from app.agent_runtime.schemas import CallTool, Complete
from app.agent_runtime.state import RunStatus
from app.agent_runtime.thinking import AgentEventThinkingSink
from app.agent_runtime.tools.contracts import ToolResult
from app.agent_runtime.tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# fakes / 装配
# ---------------------------------------------------------------------------


class FakeAgentGateway:
    """脚本化 AgentAction；记录每次 decide 收到的消息。"""

    def __init__(self, actions: list[Any], *, on_decide: Any = None) -> None:
        self.actions = list(actions)
        self.calls: list[dict[str, Any]] = []
        self.on_decide = on_decide

    async def decide(
        self, *, run, attempt_id, profile, messages, thinking_sink=None, **kwargs
    ) -> Any:
        self.calls.append(
            {
                "run_id": run.id,
                "attempt_id": attempt_id,
                "profile": profile.full_name,
                "messages": list(messages),
                "thinking_sink": thinking_sink,
                **kwargs,
            }
        )
        if self.on_decide is not None:
            await self.on_decide(run, len(self.calls))
        if not self.actions:
            raise AssertionError("fake agent gateway exhausted")
        return self.actions.pop(0)


class BlockingGateway:
    """decide 阻塞直到 release 事件；用于验证优雅关闭等待当前 Run 完成。"""

    def __init__(self, actions: list[Any], started: asyncio.Event, release: asyncio.Event) -> None:
        self.actions = list(actions)
        self.started = started
        self.release = release
        self.calls: list[dict[str, Any]] = []

    async def decide(self, *, run, attempt_id, profile, messages, thinking_sink=None, **kwargs) -> Any:
        self.calls.append({"run_id": run.id, "attempt_id": attempt_id})
        self.started.set()
        await self.release.wait()
        if not self.actions:
            raise AssertionError("blocking gateway exhausted")
        return self.actions.pop(0)


class _NoopArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str = ""


class NoopTool:
    """零积分步进工具：只返回成功，不产生任何副作用。"""

    name = "noop_calc"
    input_model = _NoopArgs
    points_cost = 0
    external_side_effect = False

    async def execute(self, context: Any, arguments: BaseModel) -> ToolResult:
        return ToolResult(status="success", safe_summary="noop ok")


class _FakeReviewerGateway:
    """executor 测试不触发 review；仅满足 ReviewerDriver 构造签名。"""

    async def decide(self, **kwargs: Any) -> Any:
        raise AssertionError("reviewer gateway should not be called")


class _FailOnceEventStream(AgentEventStream):
    """第一次终态事件插入时注入崩溃（H1：模拟进程在终态事务中途宕机）。

    崩溃点位于"Run 状态已迁移、终态事件未写"的窗口——改造后该窗口在
    同一事务内，异常必须整体回滚（Run 保持非终态、无终态事件残留）。
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.crashes_left = 1

    async def _insert_terminal_locked(self, run, event_type, payload):
        if self.crashes_left > 0:
            self.crashes_left -= 1
            raise RuntimeError("injected terminal-transaction crash")
        return await super()._insert_terminal_locked(run, event_type, payload)


@asynccontextmanager
async def _shared_session(db_session):
    """把测试的共享 AsyncSession 当作可复用的会话上下文（退出不关闭）。"""
    yield db_session


async def _make_session(db_session, user_factory, *, title: str = "执行器测试会话"):
    user = await user_factory()
    now = utc_now()
    session = AgentSession(
        id=str(uuid4()),
        user_id=user.id,
        title=title,
        status="active",
        created_at=now,
        updated_at=now,
    )
    db_session.add(session)
    await db_session.flush()
    input_msg = AgentMessage(
        id=str(uuid4()),
        session_id=session.id,
        run_id=None,
        role="user",
        content="帮我分析品牌",
        metadata_json=None,
        sequence=1,
        created_at=now,
    )
    db_session.add(input_msg)
    await db_session.flush()
    run = AgentRun(
        id=str(uuid4()),
        session_id=session.id,
        user_id=user.id,
        input_message_id=input_msg.id,
        run_kind="user",
        visibility="user",
        profile_name="session_analyst_v1",
        profile_version="v1",
        model="test-model",
        status="queued",
        decision_count=0,
        review_count=0,
        revision_count=0,
    )
    db_session.add(run)
    await db_session.flush()
    return run, session, user


def _build_executor(
    db_session,
    *,
    gateway: Any,
    worker: str = "worker",
    registry: ToolRegistry | None = None,
    lease_seconds: int = 300,
    claim_interval_seconds: float = 0.01,
    events: AgentEventStream | None = None,
) -> AgentRunExecutor:
    broker = AgentEventBroker()
    events = events or AgentEventStream(db_session, broker)
    reviewer = ReviewerDriver(db_session, _FakeReviewerGateway(), worker_id=worker)
    registry = registry or ToolRegistry()

    def engine_factory(db, worker_id, channel_permissions=()):
        return AgentEngine(
            db,
            gateway=gateway,
            registry=registry,
            events=events,
            reviewer=reviewer,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )

    return AgentRunExecutor(
        session_factory=lambda: _shared_session(db_session),
        engine_factory=engine_factory,
        worker_id=worker,
        lease_seconds=lease_seconds,
        claim_interval_seconds=claim_interval_seconds,
    )


async def _attempts(db_session, run_id: str) -> list[AgentRunAttempt]:
    return list(
        (
            await db_session.scalars(
                select(AgentRunAttempt)
                .where(AgentRunAttempt.run_id == run_id)
                .order_by(AgentRunAttempt.attempt)
            )
        ).all()
    )


# ---------------------------------------------------------------------------
# 1. queued Run 领取并执行到终态
# ---------------------------------------------------------------------------


async def test_queued_run_is_claimed_and_reaches_terminal_state(
    db_session, user_factory
) -> None:
    run, _, _ = await _make_session(db_session, user_factory)
    gateway = FakeAgentGateway([Complete(action="complete", text="分析完成")])
    executor = _build_executor(db_session, gateway=gateway)

    run_id = await executor.claim_and_process_one()

    assert run_id == run.id
    fresh = await db_session.get(AgentRun, run.id)
    assert fresh.status == RunStatus.COMPLETED
    assert fresh.completed_at is not None
    assert fresh.lease_owner is None
    assert fresh.lease_expires_at is None

    attempts = await _attempts(db_session, run.id)
    assert len(attempts) == 1
    assert attempts[0].attempt == 1
    assert attempts[0].outcome == "completed"
    assert attempts[0].ended_at is not None

    # 引擎收到了输入消息
    assert len(gateway.calls) == 1
    assert any(
        m.role == "user" and "帮我分析品牌" in m.content for m in gateway.calls[0]["messages"]
    )


async def test_executor_injects_thinking_sink_for_user_run(
    db_session, user_factory
) -> None:
    """用户可见 Run（session_analyst 主 Run）：执行器注入 AgentEventThinkingSink，
    主 Agent 真实 thinking 才能实时 SSE（§5.8/§10.5）。"""
    run, _, _ = await _make_session(db_session, user_factory)
    gateway = FakeAgentGateway([Complete(action="complete", text="分析完成")])
    executor = _build_executor(db_session, gateway=gateway)

    run_id = await executor.claim_and_process_one()

    assert run_id == run.id
    assert len(gateway.calls) == 1
    assert isinstance(gateway.calls[0]["thinking_sink"], AgentEventThinkingSink)


async def test_claim_and_process_one_returns_none_when_nothing_queued(
    db_session, user_factory
) -> None:
    gateway = FakeAgentGateway([])
    executor = _build_executor(db_session, gateway=gateway)

    assert await executor.claim_and_process_one() is None
    assert gateway.calls == []


# ---------------------------------------------------------------------------
# 2. 过期租约接管：新 worker 领取、attempt 递增、完整 Step 不重放
# ---------------------------------------------------------------------------


async def test_expired_lease_takeover_resumes_from_last_complete_step(
    db_session, user_factory
) -> None:
    run, _, _ = await _make_session(db_session, user_factory)
    repo = AgentRunRepository(db_session)
    attempt1 = await repo.begin_attempt(run.id)
    await repo.claim_lease(run.id, "worker-a", 300)
    # 模拟旧 worker 已持久化的两个完整 tool_call Step（attempt 1）。
    for seq in (1, 2):
        db_session.add(
            AgentStep(
                id=str(uuid4()),
                run_id=run.id,
                attempt_id=attempt1.id,
                sequence=seq,
                step_type="tool_call",
                input_json={"internal_tool_name": "noop_calc"},
                output_json={"status": "success"},
                status="completed",
                visibility="user",
                created_at=utc_now(),
            )
        )
    await db_session.flush()
    # 租约过期（注入过去时间）。
    row = await db_session.get(AgentRun, run.id)
    row.lease_expires_at = utc_now() - timedelta(seconds=10)
    await db_session.flush()

    registry = ToolRegistry()
    registry.register(NoopTool(), category="calculation")
    gateway = FakeAgentGateway(
        [
            CallTool(
                action="call_tool",
                internal_tool_name="noop_calc",
                arguments={"value": "x"},
                rationale="接管后继续",
            ),
            Complete(action="complete", text="接管完成"),
        ]
    )
    executor = _build_executor(db_session, gateway=gateway, registry=registry, worker="worker-b")

    run_id = await executor.claim_and_process_one()

    assert run_id == run.id
    fresh = await db_session.get(AgentRun, run.id)
    assert fresh.status == RunStatus.COMPLETED
    assert fresh.lease_owner is None
    assert fresh.paused_at is None

    # 新 worker 开启了 attempt 2，旧 attempt 已收口
    attempts = await _attempts(db_session, run.id)
    assert [attempt.attempt for attempt in attempts] == [1, 2]
    assert attempts[0].ended_at is not None
    assert attempts[1].outcome == "completed"

    # 完整 Step 不重放：attempt 2 从最后完整 Step（sequence 2）之后继续，sequence 不归零
    steps = list(
        (
            await db_session.scalars(
                select(AgentStep).where(AgentStep.run_id == run.id).order_by(AgentStep.sequence)
            )
        ).all()
    )
    assert [step.sequence for step in steps] == [1, 2, 4]
    assert steps[2].attempt_id == attempts[1].id
    assert steps[2].step_type == "tool_call"
    assert steps[2].status == "completed"
    # 引擎只新增了一个 tool_call Step，未重放 attempt 1 的两个已完成 Step
    assert all(step.sequence > 2 for step in steps if step.attempt_id == attempts[1].id)


async def test_null_lease_running_run_reuses_open_attempt(db_session, user_factory) -> None:
    """无租约（NULL）running Run：执行器直接沿用现有 open Attempt，不 pause+重建。

    这是 API resume 经 ``begin_attempt(resumed=True)`` 后的状态——新 Attempt 已
    就绪，若再 pause+重建会产生一个多余的暂停 Attempt。
    """
    run, _, _ = await _make_session(db_session, user_factory)
    repo = AgentRunRepository(db_session)
    attempt1 = await repo.begin_attempt(run.id)
    # begin_attempt 只置 running、不建租约 → lease_expires_at 为 NULL
    assert (await db_session.get(AgentRun, run.id)).lease_expires_at is None

    gateway = FakeAgentGateway([Complete(action="complete", text="恢复完成")])
    executor = _build_executor(db_session, gateway=gateway)

    run_id = await executor.claim_and_process_one()

    assert run_id == run.id
    fresh = await db_session.get(AgentRun, run.id)
    assert fresh.status == RunStatus.COMPLETED
    assert fresh.lease_owner is None
    assert fresh.paused_at is None

    # 复用 attempt 1，不新增 attempt 2
    attempts = await _attempts(db_session, run.id)
    assert [attempt.attempt for attempt in attempts] == [1]
    assert attempts[0].id == attempt1.id
    assert attempts[0].outcome == "completed"


async def test_unexpired_lease_not_reclaimed_by_other_worker(db_session, user_factory) -> None:
    run, _, _ = await _make_session(db_session, user_factory)
    repo = AgentRunRepository(db_session)
    await repo.begin_attempt(run.id)
    await repo.claim_lease(run.id, "worker-a", 300)
    # 租约仍有效：新 worker 不得领取
    gateway = FakeAgentGateway([])
    executor = _build_executor(db_session, gateway=gateway, worker="worker-b")

    assert await executor.claim_and_process_one() is None
    fresh = await db_session.get(AgentRun, run.id)
    assert fresh.status == RunStatus.RUNNING
    assert fresh.lease_owner == "worker-a"
    assert gateway.calls == []


# ---------------------------------------------------------------------------
# 3. 优雅关闭：停止领取新 Run，等待当前 Run 完成
# ---------------------------------------------------------------------------


async def test_graceful_shutdown_waits_for_inflight_run(db_session, user_factory) -> None:
    run, _, _ = await _make_session(db_session, user_factory)
    started = asyncio.Event()
    release = asyncio.Event()
    gateway = BlockingGateway([Complete(action="complete", text="完成")], started, release)
    executor = _build_executor(db_session, gateway=gateway)

    executor.start()
    try:
        # 等待引擎进入 decide（run 已被领取、正在执行）
        await asyncio.wait_for(started.wait(), timeout=5)

        stop_task = asyncio.create_task(executor.stop())
        # stop 会阻塞等待当前 in-flight Run 完成，而不是立刻取消
        await asyncio.sleep(0.05)
        assert not stop_task.done()

        release.set()
        await asyncio.wait_for(stop_task, timeout=5)
    finally:
        await executor.stop()

    fresh = await db_session.get(AgentRun, run.id)
    assert fresh.status == RunStatus.COMPLETED
    assert fresh.completed_at is not None
    assert len(gateway.calls) == 1


async def test_graceful_shutdown_stops_claiming_new_runs(db_session, user_factory) -> None:
    run, _, _ = await _make_session(db_session, user_factory)
    gateway = FakeAgentGateway([Complete(action="complete", text="完成")])
    executor = _build_executor(db_session, gateway=gateway)

    # 先 stop 再 start 前的 Run 不应被领取
    await executor.stop()
    assert await executor.claim_and_process_one() is None
    fresh = await db_session.get(AgentRun, run.id)
    assert fresh.status == RunStatus.QUEUED


# ---------------------------------------------------------------------------
# 4. 渠道权限注入（设计 §5.1）：executor 按 Run 用户查询并传给 engine_factory
# ---------------------------------------------------------------------------


async def test_executor_injects_user_channel_permissions(db_session, user_factory) -> None:
    from app.identity.models import UserChannelPermission

    run, _, user = await _make_session(db_session, user_factory)
    now = utc_now()
    db_session.add(
        UserChannelPermission(
            id=str(uuid4()),
            user_id=user.id,
            channel="bilibili",
            is_enabled=True,
            created_at=now,
            updated_at=now,
        )
    )
    db_session.add(
        UserChannelPermission(
            id=str(uuid4()),
            user_id=user.id,
            channel="douyin",
            is_enabled=False,  # 禁用渠道不得注入
            created_at=now,
            updated_at=now,
        )
    )
    await db_session.flush()

    captured: dict[str, Any] = {}
    gateway = FakeAgentGateway([Complete(action="complete", text="完成")])

    def engine_factory(db, worker_id, channel_permissions=()):
        captured["channel_permissions"] = channel_permissions
        broker = AgentEventBroker()
        events = AgentEventStream(db, broker)
        reviewer = ReviewerDriver(db, _FakeReviewerGateway(), worker_id=worker_id)
        return AgentEngine(
            db,
            gateway=gateway,
            registry=ToolRegistry(),
            events=events,
            reviewer=reviewer,
            worker_id=worker_id,
        )

    executor = AgentRunExecutor(
        session_factory=lambda: _shared_session(db_session),
        engine_factory=engine_factory,
        worker_id="worker",
        claim_interval_seconds=0.01,
    )
    run_id = await executor.claim_and_process_one()

    assert run_id == run.id
    assert frozenset(captured["channel_permissions"]) == {"bilibili"}


# ---------------------------------------------------------------------------
# 5. G1：executor 异常收口的终态事件（§5.8：恰好一个终态事件且最后）
# ---------------------------------------------------------------------------


class BrokenContextBuilder:
    """build 直接抛错的上下文构建器：模拟引擎在 decide 之外的系统崩溃。"""

    async def build(self, **kwargs: Any) -> Any:
        raise RuntimeError("context build exploded")


def _build_crashing_executor(
    db_session,
    *,
    worker: str = "worker",
    broker: AgentEventBroker | None = None,
) -> AgentRunExecutor:
    broker = broker or AgentEventBroker()
    events = AgentEventStream(db_session, broker)
    gateway = FakeAgentGateway([Complete(action="complete", text="不应到达")])
    reviewer = ReviewerDriver(db_session, _FakeReviewerGateway(), worker_id=worker)

    def engine_factory(db, worker_id, channel_permissions=()):
        return AgentEngine(
            db,
            gateway=gateway,
            registry=ToolRegistry(),
            events=events,
            reviewer=reviewer,
            worker_id=worker_id,
            context_builder=BrokenContextBuilder(),
        )

    return AgentRunExecutor(
        session_factory=lambda: _shared_session(db_session),
        engine_factory=engine_factory,
        worker_id=worker,
        claim_interval_seconds=0.01,
    )


async def _run_events(db_session, run_id: str):
    return list(
        (
            await db_session.scalars(
                select(AgentEvent)
                .where(AgentEvent.run_id == run_id)
                .order_by(AgentEvent.sequence)
            )
        ).all()
    )


async def test_engine_crash_emits_run_failed_terminal_event(
    db_session, user_factory
) -> None:
    """引擎在 decide 之外崩溃（上下文构建抛错）：executor 异常收口必须发
    run.failed 终态事件（带稳定 error_code），否则 SSE 流不结束（G1/P0）。"""
    run, _, _ = await _make_session(db_session, user_factory)
    executor = _build_crashing_executor(db_session)

    run_id = await executor.claim_and_process_one()

    assert run_id == run.id
    fresh = await db_session.get(AgentRun, run.id)
    assert fresh.status == RunStatus.FAILED
    rows = await _run_events(db_session, run.id)
    types = [row.event_type for row in rows]
    assert types[-1] == "run.failed"
    terminal = [
        row
        for row in rows
        if row.event_type in ("run.completed", "run.failed", "run.cancelled")
    ]
    assert len(terminal) == 1
    assert terminal[0].payload_json["error_code"] == "executor_error"


async def test_finalize_failed_does_not_emit_second_terminal_event(
    db_session, user_factory
) -> None:
    """引擎已自行收口 failed（decide 异常，引擎发 run.failed）后，executor
    兜底不得重复发终态事件——同一 Run 全局恰好一个（G1）。"""
    run, _, _ = await _make_session(db_session, user_factory)
    # actions 为空：decide 抛 AssertionError，引擎自己 _fail_run 收口并发 run.failed
    gateway = FakeAgentGateway([])
    executor = _build_executor(db_session, gateway=gateway)

    run_id = await executor.claim_and_process_one()

    assert run_id == run.id
    fresh = await db_session.get(AgentRun, run.id)
    assert fresh.status == RunStatus.FAILED
    # 模拟 executor 兜底再调一次（如引擎收口后又抛异常）：不得出现第二个终态事件
    await executor._finalize_failed(db_session, run.id, "worker")
    rows = await _run_events(db_session, run.id)
    terminal = [
        row
        for row in rows
        if row.event_type in ("run.completed", "run.failed", "run.cancelled")
    ]
    assert len(terminal) == 1
    assert terminal[0].event_type == "run.failed"
    assert terminal[0].payload_json["error_code"] == "model_error"


async def test_finalize_failed_skips_when_lease_held_by_other_worker(
    db_session, user_factory
) -> None:
    """租约被其他 worker 活跃持有（已接管）：旧 worker 的 executor 兜底既不改写
    Run 状态也不发终态事件——终态由接管方负责（A4 租约闸门，G1 保持）。"""
    run, _, _ = await _make_session(db_session, user_factory)
    repo = AgentRunRepository(db_session)
    await repo.begin_attempt(run.id)
    assert await repo.claim_lease(run.id, "worker-a", 300)

    gateway = FakeAgentGateway([])
    executor = _build_executor(db_session, gateway=gateway, worker="worker-b")
    await executor._finalize_failed(db_session, run.id, "worker-b")

    fresh = await db_session.get(AgentRun, run.id)
    assert fresh.status == RunStatus.RUNNING
    assert fresh.lease_owner == "worker-a"
    rows = await _run_events(db_session, run.id)
    assert [
        row
        for row in rows
        if row.event_type in ("run.completed", "run.failed", "run.cancelled")
    ] == []


async def test_terminal_settle_crash_rolls_back_and_executor_settles_failed(
    db_session, user_factory
) -> None:
    """H1 崩溃注入：complete 的终态事务（Run 迁移 + run.completed 事件）中途
    崩溃——整体回滚，Run 保持非终态且无 run.completed 残留；executor 兜底
    以恰好一个 run.failed 终态事件收口（message.completed 顺序不变）。"""
    run, _, _ = await _make_session(db_session, user_factory)
    events = _FailOnceEventStream(db_session, AgentEventBroker())
    gateway = FakeAgentGateway([Complete(action="complete", text="分析完成")])
    executor = _build_executor(db_session, gateway=gateway, events=events)

    run_id = await executor.claim_and_process_one()

    assert run_id == run.id
    fresh = await db_session.get(AgentRun, run.id)
    assert fresh.status == RunStatus.FAILED
    rows = await _run_events(db_session, run.id)
    types = [row.event_type for row in rows]
    terminal = [
        row
        for row in rows
        if row.event_type in ("run.completed", "run.failed", "run.cancelled")
    ]
    # 第一次终态事务整体回滚：无 run.completed 残留、终态事件恰好一个。
    assert len(terminal) == 1
    assert terminal[0].event_type == "run.failed"
    assert terminal[0].payload_json["error_code"] == "executor_error"
    # message.completed 在终态事务之前已提交（顺序不变），回滚不波及。
    assert "message.completed" in types
    assert types.index("message.completed") < types.index("run.failed")


async def test_live_stream_executor_crash_ends_with_run_failed() -> None:
    """跨会话在线消费（reader 独立会话）：executor 异常收口路径的 SSE 流以
    run.failed 终态事件收口——流必须结束（§5.8/G1 P0）。"""
    from app.db.session import SessionFactory

    from tests.agent_runtime.test_engine import (
        _create_committed_agent_run,
        _purge_committed_agent_run,
    )

    user_id, session_id, run_id = await _create_committed_agent_run()
    broker = AgentEventBroker()

    def engine_factory(db, worker_id, channel_permissions=()):
        return AgentEngine(
            db,
            gateway=FakeAgentGateway([Complete(action="complete", text="不应到达")]),
            registry=ToolRegistry(),
            events=AgentEventStream(db, broker),
            reviewer=ReviewerDriver(db, _FakeReviewerGateway(), worker_id=worker_id),
            worker_id=worker_id,
            context_builder=BrokenContextBuilder(),
        )

    executor = AgentRunExecutor(
        session_factory=SessionFactory,
        engine_factory=engine_factory,
        worker_id="worker",
        lease_seconds=300,
        claim_interval_seconds=0.01,
    )
    try:
        received: list[str] = []

        async def consume() -> None:
            async with SessionFactory() as reader:
                stream = AgentEventStream(reader, broker)
                async for event in stream.stream(run_id, user_id, last_event_id=0):
                    received.append(event.event_type)

        consumer = asyncio.create_task(consume())
        await asyncio.sleep(0.05)  # reader 完成重放并进入 broker 等待
        processed = await executor.claim_and_process_one()
        assert processed == run_id
        # executor 修复前：流永远等不到终态事件，这里会超时失败
        await asyncio.wait_for(consumer, timeout=5)
    finally:
        await _purge_committed_agent_run(user_id, session_id, run_id)

    assert received[-1] == "run.failed"
    assert sum(
        1 for t in received if t in ("run.completed", "run.failed", "run.cancelled")
    ) == 1


async def test_takeover_feeds_transcript_user_question_to_memory_header(
    db_session, user_factory
) -> None:
    """接管恢复（G3）：transcript 的显式用户问题锚点经执行器传给引擎——
    恢复后 Memory Header 的 current_user_message 是触发消息，而不是会话尾部
    tool_result 回放 JSON（role="user"）。"""
    run, _, _ = await _make_session(db_session, user_factory)
    repo = AgentRunRepository(db_session)
    attempt1 = await repo.begin_attempt(run.id)
    await repo.claim_lease(run.id, "worker-a", 300)
    # 崩溃残留：一个已完成 tool_call Step；回放后对话尾部是 tool_result 用户消息。
    db_session.add(
        AgentStep(
            id=str(uuid4()),
            run_id=run.id,
            attempt_id=attempt1.id,
            sequence=1,
            step_type="tool_call",
            input_json={"internal_tool_name": "noop_calc", "arguments": {}},
            output_json={
                "status": "success",
                "safe_summary": "noop ok",
                "evidence_id": None,
                "cursor": None,
                "truncated": False,
                "error_type": None,
            },
            status="completed",
            visibility="user",
            created_at=utc_now(),
        )
    )
    await db_session.flush()
    # 租约过期 → worker-b 接管。
    run.lease_expires_at = utc_now() - timedelta(seconds=1)
    await db_session.flush()

    gateway = FakeAgentGateway([Complete(action="complete", text="继续分析完成")])
    executor = _build_executor(db_session, gateway=gateway)

    outcome = await executor.process_run(run.id, worker_id="worker-b")

    assert outcome == RunStatus.COMPLETED
    assert gateway.calls
    header = json.loads(gateway.calls[0]["messages"][0].content)
    assert header["current_user_message"] == "帮我分析品牌"
    assert "tool_result" not in header["current_user_message"]
