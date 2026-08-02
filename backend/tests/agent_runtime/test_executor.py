"""AgentRunExecutor 集成测试（Task 15 / 设计文档 §七 / §八）。

覆盖：
1. queued Run 被 worker 领取、经 Task 14 引擎执行并到达终态；
2. 过期租约接管：新 worker 领取、新建 Attempt（attempt 递增）、从最后完整
   Step 继续（完整 Step 不重放，sequence 不归零）；
3. 优雅关闭：停止领取新 Run，等待当前 Run 在事务安全点完成。
"""

from __future__ import annotations

import asyncio
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
) -> AgentRunExecutor:
    broker = AgentEventBroker()
    events = AgentEventStream(db_session, broker)
    reviewer = ReviewerDriver(db_session, _FakeReviewerGateway(), worker_id=worker)
    registry = registry or ToolRegistry()

    def engine_factory(db):
        return AgentEngine(
            db,
            gateway=gateway,
            registry=registry,
            events=events,
            reviewer=reviewer,
            worker_id=worker,
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
