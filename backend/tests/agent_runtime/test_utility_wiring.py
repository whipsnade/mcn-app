"""Utility 接线测试（v3 加固设计 §6.4）：标题 / Run 摘要 / 建议的触发时机。

utility.py 的 UtilityRunner 只提供三个任务入口，本文件覆盖生产接线：

1. 会话首条用户消息提交后 best-effort 触发 session_title（仅首条；后续
   消息不再触发；标题被用户重命名过不得覆盖——重命名保护本身在
   test_utility.py 覆盖）；
2. 用户主 Run（session_analyst_v1）到达终态/澄清等待（completed / failed /
   cancelled / clarification_requested）后，executor 在 settle 成功之后异步
   触发 run_summary + suggestions：摘要落 memory_entries，建议落完成消息
   metadata（与 complete 动作自带 suggestions 同一前端契约）；
3. utility 失败是 best-effort：Run 结果与事件流（恰好一个终态事件）不变；
4. 内部/辅助 Run（run_kind=internal、kol_detail_v1）不触发；
5. API 立即取消（queued → cancelled）同样触发。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.agent_runtime.engine import AgentEngine
from app.agent_runtime.events import AgentEventBroker, AgentEventStream
from app.agent_runtime.executor import AgentRunExecutor
from app.agent_runtime.models import (
    AgentEvent,
    AgentMessage,
    AgentRun,
    AgentSession,
    MemoryEntry,
)
from app.agent_runtime.repository import utc_now
from app.agent_runtime.schemas import AskUser, Complete
from app.agent_runtime.state import RunStatus
from app.agent_runtime.tools.registry import ToolRegistry
from app.agent_runtime.utility import UtilityDispatcher, UtilityRunner
from app.db.session import get_db
from app.main import create_app


# ---------------------------------------------------------------------------
# fakes / 装配
# ---------------------------------------------------------------------------


class RecordingUtilityDispatcher:
    """记录触发调用的假 dispatcher；不真正执行 utility。"""

    def __init__(self) -> None:
        self.title_calls: list[dict[str, str]] = []
        self.followup_run_ids: list[str] = []

    def schedule_session_title(self, *, session_id: str, user_id: str) -> None:
        self.title_calls.append({"session_id": session_id, "user_id": user_id})

    def schedule_run_followups(self, *, run_id: str) -> None:
        self.followup_run_ids.append(run_id)


class FakeAgentGateway:
    """脚本化 AgentAction；actions 为空时 decide 抛错（引擎自行收口 failed）。"""

    def __init__(self, actions: list[Any]) -> None:
        self.actions = list(actions)

    async def decide(self, *, run, attempt_id, profile, messages, thinking_sink=None, **kwargs) -> Any:
        if not self.actions:
            raise AssertionError("fake agent gateway exhausted")
        return self.actions.pop(0)


class FakeUtilityGateway:
    """脚本化 UtilityDecision JSON。"""

    def __init__(self, outcomes: list[dict[str, Any]]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    async def decide(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self.outcomes:
            raise AssertionError("fake utility gateway exhausted")
        return self.outcomes.pop(0)


class BoomUtilityGateway:
    async def decide(self, **kwargs: Any) -> Any:
        raise RuntimeError("utility model call failed")


class FakeExecutor:
    """API 测试用假执行器：Run 停留 queued，只记录 submit。"""

    def __init__(self) -> None:
        self.submitted: list[str] = []

    def submit(self, run_id: str) -> None:
        self.submitted.append(run_id)


@asynccontextmanager
async def _shared_session(db_session):
    """把测试的共享 AsyncSession 当作可复用的会话上下文（退出不关闭）。"""
    yield db_session


def _build_executor(
    db_session,
    *,
    agent_gateway: Any,
    utility_dispatcher: Any = None,
    worker: str = "worker",
) -> AgentRunExecutor:
    broker = AgentEventBroker()
    events = AgentEventStream(db_session, broker)

    def engine_factory(db, worker_id, channel_permissions=()):
        return AgentEngine(
            db,
            gateway=agent_gateway,
            registry=ToolRegistry(),
            events=events,
            worker_id=worker_id,
        )

    return AgentRunExecutor(
        session_factory=lambda: _shared_session(db_session),
        engine_factory=engine_factory,
        worker_id=worker,
        lease_seconds=300,
        utility_dispatcher=utility_dispatcher,
    )


async def _make_run(
    db_session,
    user_factory,
    *,
    run_kind: str = "user",
    visibility: str = "user",
    profile_name: str = "session_analyst_v1",
    status: str = "queued",
    cancel_requested: bool = False,
):
    user = await user_factory()
    now = utc_now()
    session = AgentSession(
        id=str(uuid4()),
        user_id=user.id,
        title="新会话1",
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
        run_kind=run_kind,
        visibility=visibility,
        profile_name=profile_name,
        profile_version="v1",
        model="test-model",
        status=status,
        cancel_requested=cancel_requested,
        decision_count=0,
        review_count=0,
        revision_count=0,
    )
    db_session.add(run)
    await db_session.flush()
    return run, session, user


def _real_dispatcher(db_session, utility_gateway: Any) -> UtilityDispatcher:
    """绑定共享会话 + 脚本化 utility 网关的真实 dispatcher（需显式 start）。"""

    def runner_factory(db):
        return UtilityRunner(db=db, gateway=utility_gateway)

    dispatcher = UtilityDispatcher(
        session_factory=lambda: _shared_session(db_session),
        runner_factory=runner_factory,
    )
    dispatcher.start()
    return dispatcher


async def _internal_runs(db_session, parent_run_id: str) -> list[AgentRun]:
    return list(
        (
            await db_session.scalars(
                select(AgentRun).where(AgentRun.parent_run_id == parent_run_id)
            )
        ).all()
    )


async def _run_events(db_session, run_id: str) -> list[AgentEvent]:
    return list(
        (
            await db_session.scalars(
                select(AgentEvent)
                .where(AgentEvent.run_id == run_id)
                .order_by(AgentEvent.sequence)
            )
        ).all()
    )


# ---------------------------------------------------------------------------
# 1. API：首条用户消息触发 session_title（仅首条）
# ---------------------------------------------------------------------------


async def _make_agent_client(db_session, phone: str, *, executor: Any, dispatcher: Any):
    from app.agent_runtime.router import get_agent_executor, get_utility_dispatcher

    app = create_app()

    async def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_agent_executor] = lambda: executor
    app.dependency_overrides[get_utility_dispatcher] = lambda: dispatcher
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    login = await client.post(
        "/api/v1/auth/mock/sms/login", json={"phone": phone, "code": "000000"}
    )
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
    return client


async def test_first_message_triggers_session_title_only_once(db_session) -> None:
    executor = FakeExecutor()
    dispatcher = RecordingUtilityDispatcher()
    client = await _make_agent_client(
        db_session, "13800000101", executor=executor, dispatcher=dispatcher
    )
    me = await client.get("/api/v1/users/me")
    user_id = me.json()["id"]
    created = await client.post("/api/v1/agent/sessions", json={})
    session_id = created.json()["id"]

    first = await client.post(
        f"/api/v1/agent/sessions/{session_id}/messages", json={"content": "帮我分析瑞幸"}
    )
    assert first.status_code == 201, first.text
    assert dispatcher.title_calls == [{"session_id": session_id, "user_id": user_id}]

    # 第一条 Run 终态后再发第二条消息：不再触发标题生成。
    run = await db_session.get(AgentRun, first.json()["run_id"])
    run.status = "completed"
    run.completed_at = utc_now()
    session = await db_session.get(AgentSession, session_id)
    session.active_run_id = None
    await db_session.flush()
    second = await client.post(
        f"/api/v1/agent/sessions/{session_id}/messages", json={"content": "继续深入"}
    )
    assert second.status_code == 201, second.text
    assert len(dispatcher.title_calls) == 1
    await client.aclose()


async def test_immediate_cancel_triggers_run_followups(db_session) -> None:
    """queued Run 被立即取消（settle_terminal 直接收口）也触发摘要/建议。"""
    executor = FakeExecutor()
    dispatcher = RecordingUtilityDispatcher()
    client = await _make_agent_client(
        db_session, "13800000102", executor=executor, dispatcher=dispatcher
    )
    created = await client.post("/api/v1/agent/sessions", json={})
    session_id = created.json()["id"]
    message = await client.post(
        f"/api/v1/agent/sessions/{session_id}/messages", json={"content": "帮我分析瑞幸"}
    )
    run_id = message.json()["run_id"]

    cancel = await client.post(f"/api/v1/agent/runs/{run_id}/cancel")
    assert cancel.status_code == 200, cancel.text
    assert cancel.json()["status"] == "cancelled"
    assert dispatcher.followup_run_ids == [run_id]
    await client.aclose()


# ---------------------------------------------------------------------------
# 2. executor：用户主 Run 终态触发 run_summary + suggestions
# ---------------------------------------------------------------------------


async def test_completed_run_triggers_summary_and_suggestions(db_session, user_factory) -> None:
    run, session, _user = await _make_run(db_session, user_factory)
    agent_gateway = FakeAgentGateway(
        [Complete(action="complete", text="分析完成", suggestions=None)]
    )
    utility_gateway = FakeUtilityGateway(
        [
            {"task": "run_summary", "summary": "用户完成了瑞幸品牌分析"},
            {"task": "suggestions", "suggestions": ["继续看竞品", "拆解受众地域"]},
        ]
    )
    dispatcher = _real_dispatcher(db_session, utility_gateway)
    executor = _build_executor(
        db_session, agent_gateway=agent_gateway, utility_dispatcher=dispatcher
    )

    outcome = await executor.process_run(run.id)
    await dispatcher.wait_idle()

    assert outcome == RunStatus.COMPLETED
    # 摘要落 memory_entries（run_summary）
    entry = await db_session.scalar(
        select(MemoryEntry).where(
            MemoryEntry.memory_type == "run_summary", MemoryEntry.source_run_id == run.id
        )
    )
    assert entry is not None
    assert entry.content_json["summary"] == "用户完成了瑞幸品牌分析"
    # 建议落完成消息 metadata（与 complete 动作 suggestions 同一前端契约）
    assistant = await db_session.scalar(
        select(AgentMessage).where(
            AgentMessage.run_id == run.id, AgentMessage.role == "assistant"
        )
    )
    assert assistant is not None
    assert assistant.metadata_json["suggestions"] == ["继续看竞品", "拆解受众地域"]
    # 两个 internal 子 Run（summary + suggestions）挂在父 Run 下并成功收口
    internals = await _internal_runs(db_session, run.id)
    assert len(internals) == 2
    assert {item.status for item in internals} == {"completed"}
    assert {item.run_kind for item in internals} == {"internal"}
    # 父 Run 不受影响
    fresh = await db_session.get(AgentRun, run.id)
    assert fresh.status == "completed"


async def test_failed_run_triggers_followups(db_session, user_factory) -> None:
    """decide 抛错 → 引擎收口 failed（终态）后同样触发摘要/建议。"""
    run, _, _ = await _make_run(db_session, user_factory)
    dispatcher = RecordingUtilityDispatcher()
    executor = _build_executor(
        db_session, agent_gateway=FakeAgentGateway([]), utility_dispatcher=dispatcher
    )

    outcome = await executor.process_run(run.id)

    assert outcome == RunStatus.FAILED
    assert dispatcher.followup_run_ids == [run.id]


async def test_clarification_requested_triggers_followups(db_session, user_factory) -> None:
    run, _, _ = await _make_run(db_session, user_factory)
    dispatcher = RecordingUtilityDispatcher()
    agent_gateway = FakeAgentGateway(
        [AskUser(action="ask_user", question="分析哪个品牌？", options=["瑞幸", "库迪"])]
    )
    executor = _build_executor(
        db_session, agent_gateway=agent_gateway, utility_dispatcher=dispatcher
    )

    outcome = await executor.process_run(run.id)

    assert outcome == RunStatus.CLARIFICATION_REQUESTED
    assert dispatcher.followup_run_ids == [run.id]


async def test_cancel_pending_run_settled_by_executor_triggers_followups(
    db_session, user_factory
) -> None:
    """取消待处理孤儿（cancel_requested + 无租约 running）由 executor 收口
    cancelled 后触发摘要/建议。"""
    run, _, _ = await _make_run(
        db_session, user_factory, status="running", cancel_requested=True
    )
    dispatcher = RecordingUtilityDispatcher()
    executor = _build_executor(
        db_session, agent_gateway=FakeAgentGateway([]), utility_dispatcher=dispatcher
    )

    await executor.process_run(run.id)

    fresh = await db_session.get(AgentRun, run.id)
    assert fresh.status == "cancelled"
    assert dispatcher.followup_run_ids == [run.id]


# ---------------------------------------------------------------------------
# 3. best-effort：utility 失败不改变 Run 结果与事件流
# ---------------------------------------------------------------------------


async def test_utility_failure_keeps_run_result_and_event_stream(
    db_session, user_factory
) -> None:
    run, _, _ = await _make_run(db_session, user_factory)
    agent_gateway = FakeAgentGateway(
        [Complete(action="complete", text="分析完成", suggestions=None)]
    )
    dispatcher = _real_dispatcher(db_session, BoomUtilityGateway())
    executor = _build_executor(
        db_session, agent_gateway=agent_gateway, utility_dispatcher=dispatcher
    )

    outcome = await executor.process_run(run.id)
    await dispatcher.wait_idle()

    assert outcome == RunStatus.COMPLETED
    fresh = await db_session.get(AgentRun, run.id)
    assert fresh.status == "completed"
    # 事件流不变：恰好一个 run.completed 终态事件，且无 utility 副作用事件
    events = await _run_events(db_session, run.id)
    terminal = [
        row
        for row in events
        if row.event_type in ("run.completed", "run.failed", "run.cancelled")
    ]
    assert len(terminal) == 1
    assert terminal[0].event_type == "run.completed"
    # 无残留摘要
    assert (
        await db_session.scalar(
            select(MemoryEntry).where(MemoryEntry.source_run_id == run.id)
        )
    ) is None
    # 内部 Utility Run 以 failed 收口，不污染父 Run
    internals = await _internal_runs(db_session, run.id)
    assert len(internals) == 2
    assert {item.status for item in internals} == {"failed"}
    assert {item.error_code for item in internals} == {"utility_failed"}


# ---------------------------------------------------------------------------
# 4. 内部/辅助 Run 不触发
# ---------------------------------------------------------------------------


async def test_internal_run_does_not_trigger_followups(db_session, user_factory) -> None:
    run, _, _ = await _make_run(
        db_session, user_factory, run_kind="internal", visibility="internal"
    )
    dispatcher = RecordingUtilityDispatcher()
    agent_gateway = FakeAgentGateway(
        [Complete(action="complete", text="内部完成", suggestions=None)]
    )
    executor = _build_executor(
        db_session, agent_gateway=agent_gateway, utility_dispatcher=dispatcher
    )

    outcome = await executor.process_run(run.id)

    assert outcome == RunStatus.COMPLETED
    assert dispatcher.followup_run_ids == []


async def test_kol_detail_run_does_not_trigger_followups(db_session, user_factory) -> None:
    """kol_detail_v1 是 run_kind=user/visibility=user 的辅助 Run：崩溃恢复会
    被 executor 领取执行，但终态不得触发会话级摘要/建议。"""
    run, _, _ = await _make_run(db_session, user_factory, profile_name="kol_detail_v1")
    dispatcher = RecordingUtilityDispatcher()
    agent_gateway = FakeAgentGateway(
        [Complete(action="complete", text="达人详情完成", suggestions=None)]
    )
    executor = _build_executor(
        db_session, agent_gateway=agent_gateway, utility_dispatcher=dispatcher
    )

    outcome = await executor.process_run(run.id)

    assert outcome == RunStatus.COMPLETED
    assert dispatcher.followup_run_ids == []
