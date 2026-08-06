"""崩溃注入接管 / 历史 reviewing 收口 / 租约心跳 / 取消收口测试（v3 加固 §5.4/§5.5 / A4；
直接发布改造 Task 4）。

崩溃注入点覆盖：decide 后、MCP 外发后、settle 前（Step 未更新）、publish 前。
接管后必须满足：

- transcript 含此前工具结果（settled 回放 evidence_id + 结构化预览）；
- 绝不重复外发（transport 零新调用）、绝不重复扣费（钱包/预留不变）；
- 模型重发相同调用时复用原 Step（同一 logical_call_id 幂等回放）；
- 历史 reviewing Run（部署前复核期间崩溃）：恢复扫描收口 failed
  （LEGACY_REVIEWING_UNSUPPORTED），保留 Draft，不再启动 Reviewer；
- 长 decide/MCP 期间心跳续租；租约被接管后旧 worker 不发布、不写终态；
- 取消在 decide 后 / publish 后收口为恰好一个 run.cancelled 事件；
- run.resumed 事件区分 resumed_by（system 接管 / user 主动恢复）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select

from app.agent_artifacts.models import (
    AgentArtifactVersion,
    ArtifactDraft,
)
from app.agent_artifacts.service import ArtifactService
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
    AgentToolCall,
)
from app.agent_runtime.profiles import get_profile
from app.agent_runtime.recovery import RecoveryLoop
from app.agent_runtime.repository import AgentRunRepository, utc_now
from app.agent_runtime.schemas import AskUser, CallTool, Complete, PublishArtifacts
from app.agent_runtime.state import RunStatus
from app.agent_runtime.tools.mcp import AgentMcpTool, logical_call_id_for
from app.agent_runtime.tools.registry import McpCatalogEntry, ToolRegistry
from app.billing.service import WalletService
from app.mcp_gateway.contracts import DataTapService
from app.mcp_gateway.transport import RemoteToolResult
from app.mcp_gateway.validation import canonical_json_bytes

from tests.agent_artifacts.payload_fixtures import insight_payload

INPUT_SCHEMA = {
    "type": "object",
    "properties": {"keyword": {"type": "string"}},
    "required": ["keyword"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"result": {"type": "string"}},
    "required": ["result"],
    "additionalProperties": False,
}
OK_PAYLOAD = {
    "result": json.dumps(
        {"rows": [{"keyword": "美妆", "volume": 123}], "total": 1},
        ensure_ascii=False,
    )
}
INTERNAL_NAME = "query_analysis_data"
REMOTE_NAME = "datatap.insight.query.analysis.v1"

PAYLOAD_V1 = insight_payload()


# ---------------------------------------------------------------------------
# fakes / 装配
# ---------------------------------------------------------------------------


class FakeAgentGateway:
    def __init__(self, actions: list[Any], *, on_decide: Any = None) -> None:
        self.actions = list(actions)
        self.calls: list[dict[str, Any]] = []
        self.on_decide = on_decide

    async def decide(
        self, *, run, attempt_id, profile, messages, thinking_sink=None, **kwargs
    ) -> Any:
        self.calls.append(
            {"run_id": run.id, "attempt_id": attempt_id, "messages": list(messages)}
        )
        if self.on_decide is not None:
            await self.on_decide(run, len(self.calls))
        if not self.actions:
            raise AssertionError("fake agent gateway exhausted")
        return self.actions.pop(0)


class BlockingGateway:
    """decide 阻塞到 release：模拟长模型调用（心跳覆盖窗口）。"""

    def __init__(
        self, actions: list[Any], started: asyncio.Event, release: asyncio.Event
    ) -> None:
        self.actions = list(actions)
        self.started = started
        self.release = release

    async def decide(self, *, run, attempt_id, profile, messages, thinking_sink=None, **kwargs) -> Any:
        self.started.set()
        await self.release.wait()
        if not self.actions:
            raise AssertionError("blocking gateway exhausted")
        return self.actions.pop(0)


class FakeMcpTransport:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[DataTapService, str, dict[str, Any]]] = []

    async def call_tool(self, service: DataTapService, remote_name: str, arguments: Any):
        self.calls.append((service, remote_name, dict(arguments)))
        if not self.outcomes:
            raise AssertionError("unexpected transport dispatch")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _NoopArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str = ""


def _ok_result() -> RemoteToolResult:
    return RemoteToolResult(
        structured_content=OK_PAYLOAD, is_error=False, upstream_request_id="req-ok"
    )


@asynccontextmanager
async def _shared_session(db_session):
    yield db_session


def _mcp_registry(db_session, transport: FakeMcpTransport) -> ToolRegistry:
    entry = McpCatalogEntry(
        internal_tool_name=INTERNAL_NAME,
        service_slug="insight-cube-mcp",
        reviewed_description="query analysis data",
        input_schema_json=INPUT_SCHEMA,
        review_status="approved",
        is_enabled=True,
    )
    return ToolRegistry(
        catalog_source=[entry],
        mcp_executor_factory=lambda row: AgentMcpTool(
            internal_name=row.internal_tool_name,
            service=DataTapService.INSIGHT_CUBE,
            remote_name=REMOTE_NAME,
            input_schema=row.input_schema_json,
            output_schema=OUTPUT_SCHEMA,
            db_session=db_session,
            transport=transport,
            session_factory=lambda: _shared_session(db_session),
        ),
    )


async def _make_session(db_session, user_factory, *, message: str = "帮我分析品牌"):
    user = await user_factory()
    now = utc_now()
    session = AgentSession(
        id=str(uuid4()),
        user_id=user.id,
        title="接管测试会话",
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
        content=message,
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
    input_msg.run_id = run.id
    await db_session.flush()
    return run, session, user


async def _start_attempt(db_session, run: AgentRun, *, worker: str, lease_seconds: int = 300):
    """queued → running + Attempt 1 + 租约（模拟旧 worker 已领取）。"""
    repo = AgentRunRepository(db_session)
    attempt = await repo.begin_attempt(run.id)
    await repo.claim_lease(run.id, worker, lease_seconds)
    return attempt


def _build_executor(
    db_session,
    *,
    gateway: Any,
    registry: ToolRegistry | None = None,
    worker: str = "worker-b",
    lease_seconds: int = 300,
) -> AgentRunExecutor:
    broker = AgentEventBroker()
    events = AgentEventStream(db_session, broker)

    def engine_factory(db, worker_id, channel_permissions=()):
        return AgentEngine(
            db,
            gateway=gateway,
            registry=registry or ToolRegistry(),
            events=events,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )

    return AgentRunExecutor(
        session_factory=lambda: _shared_session(db_session),
        engine_factory=engine_factory,
        worker_id=worker,
        lease_seconds=lease_seconds,
        claim_interval_seconds=0.01,
    )


def _build_recovery(db_session, *, executor: AgentRunExecutor) -> RecoveryLoop:
    return RecoveryLoop(
        executor=executor,
        session_factory=lambda: _shared_session(db_session),
        tool_factory=lambda _db, _call: None,
        worker_id="recovery-worker",
        lease_seconds=300,
        interval_seconds=0.01,
        clock=utc_now,
    )


def _tool_results(messages: list[Any]) -> list[dict]:
    """从 decide 收到的消息中抽出 tool_result 负载（记忆头/触发消息自动跳过）。"""
    results = []
    for message in messages:
        if message.role != "user":
            continue
        try:
            payload = json.loads(message.content)
        except ValueError:
            continue
        if isinstance(payload, dict) and "tool_result" in payload:
            results.append(payload["tool_result"])
    return results


async def _events(db_session, run_id: str) -> list[AgentEvent]:
    return list(
        (
            await db_session.scalars(
                select(AgentEvent)
                .where(AgentEvent.run_id == run_id)
                .order_by(AgentEvent.sequence)
            )
        ).all()
    )


def _expire_lease(row: AgentRun) -> None:
    row.lease_expires_at = utc_now() - timedelta(seconds=10)


async def _tool_steps(db_session, run_id: str) -> list[AgentStep]:
    return list(
        (
            await db_session.scalars(
                select(AgentStep)
                .where(AgentStep.run_id == run_id, AgentStep.step_type == "tool_call")
                .order_by(AgentStep.sequence)
            )
        ).all()
    )


# ---------------------------------------------------------------------------
# 1. settle 后崩溃：transcript 回放结果，接管不重复外发/扣费
# ---------------------------------------------------------------------------


async def test_takeover_after_settle_replays_evidence_and_never_redispatches(
    db_session, user_factory
) -> None:
    """崩溃现场：Step completed + 调用 settled + Evidence 已落库。

    新 worker 接管后模型必须看到此前工具结果（evidence_id + 预览），直接
    complete：零新外发、零新扣费。
    """
    run, session, user = await _make_session(db_session, user_factory)
    await WalletService(db_session).ensure_welcome_grant(user.id)
    attempt1 = await _start_attempt(db_session, run, worker="worker-a")

    # 旧 worker 真实执行一次 MCP（settled + evidence + 扣 10 分）。
    transport_a = FakeMcpTransport([_ok_result()])
    registry_a = _mcp_registry(db_session, transport_a)
    step1 = AgentStep(
        id=str(uuid4()),
        run_id=run.id,
        attempt_id=attempt1.id,
        sequence=2,
        step_type="tool_call",
        input_json={"internal_tool_name": INTERNAL_NAME, "arguments": {"keyword": "美妆"}},
        output_json=None,
        status="running",
        visibility="user",
        created_at=utc_now(),
    )
    db_session.add(step1)
    await db_session.flush()
    result = await registry_a.execute(
        internal_name=INTERNAL_NAME,
        arguments={"keyword": "美妆"},
        user_id=user.id,
        session_id=session.id,
        run_id=run.id,
        profile=get_profile("session_analyst_v1"),
        step_id=step1.id,
    )
    assert result.status == "success"
    step1.status = "completed"
    step1.output_json = result.model_dump()
    await db_session.flush()
    wallet = await WalletService(db_session).get_wallet(user.id)
    assert (wallet.balance, wallet.reserved) == (990, 0)

    # 崩溃注入：租约过期（旧 worker 死亡）。
    _expire_lease(await db_session.get(AgentRun, run.id))
    await db_session.flush()

    # 新 worker 接管：transport 空 outcomes——任何外发都会 AssertionError。
    gateway = FakeAgentGateway([Complete(action="complete", text="基于已查数据给结论")])
    executor = _build_executor(
        db_session,
        gateway=gateway,
        registry=_mcp_registry(db_session, FakeMcpTransport([])),
    )

    run_id = await executor.claim_and_process_one()

    assert run_id == run.id
    fresh = await db_session.get(AgentRun, run.id)
    assert fresh.status == RunStatus.COMPLETED
    # 模型第一轮 decide 就看到了此前工具结果（evidence_id + 结构化预览）
    tool_results = _tool_results(gateway.calls[0]["messages"])
    assert len(tool_results) == 1
    assert tool_results[0]["status"] == "success"
    assert tool_results[0]["evidence_id"] == result.evidence_id
    # 统一模型视图：归一化预览含 volume 数据（keyword 进入 unmapped_fields）。
    assert "volume" in tool_results[0]["summary"]
    # 接管后零新外发、零新扣费
    assert (await WalletService(db_session).get_wallet(user.id)).balance == 990
    calls = (
        await db_session.scalars(
            select(AgentToolCall).where(AgentToolCall.run_id == run.id)
        )
    ).all()
    assert len(calls) == 1
    assert calls[0].status == "settled"
    # 系统接管的 run.resumed 带 resumed_by="system"
    resumed = [event for event in await _events(db_session, run.id) if event.event_type == "run.resumed"]
    assert len(resumed) == 1
    assert resumed[0].payload_json["resumed_by"] == "system"


# ---------------------------------------------------------------------------
# 2. 外发后崩溃（settle 前）：复用原 Step，logical_call_id 幂等回放
# ---------------------------------------------------------------------------


async def _seed_unknown_call(
    db_session, user_id: str, run: AgentRun, step: AgentStep
) -> AgentToolCall:
    """外发后崩溃现场：agent_tool_calls unknown（预留未结算）。"""
    args_hash = hashlib.sha256(canonical_json_bytes({"keyword": "美妆"})).hexdigest()
    call = AgentToolCall(
        id=str(uuid4()),
        run_id=run.id,
        step_id=step.id,
        logical_call_id=logical_call_id_for(run.id, INTERNAL_NAME, args_hash),
        service=DataTapService.INSIGHT_CUBE.value,
        internal_tool_name=INTERNAL_NAME,
        arguments_json={"keyword": "美妆"},
        arguments_hash=args_hash,
        status="unknown",
        points_reserved=10,
        error_type="result_unknown",
        safe_error_message="gateway timeout 504",
        started_at=utc_now(),
    )
    db_session.add(call)
    await WalletService(db_session).reserve(
        user_id,
        10,
        f"agent-mcp:{call.logical_call_id}:reserve",
        call.id,
        reference_type="agent_tool_call",
    )
    await db_session.flush()
    return call


async def test_takeover_after_dispatch_reuses_step_and_never_redispatches(
    db_session, user_factory
) -> None:
    """崩溃现场：Step running + 调用 unknown（外发后、settle 前）。

    接管后 transcript 回放 unknown 结果；模型重发**相同**调用时引擎复用原
    Step（同一 logical_call_id），协调器幂等回放——零新外发、零新预留。
    """
    run, session, user = await _make_session(db_session, user_factory)
    await WalletService(db_session).ensure_welcome_grant(user.id)
    attempt1 = await _start_attempt(db_session, run, worker="worker-a")
    step1 = AgentStep(
        id=str(uuid4()),
        run_id=run.id,
        attempt_id=attempt1.id,
        sequence=2,
        step_type="tool_call",
        input_json={"internal_tool_name": INTERNAL_NAME, "arguments": {"keyword": "美妆"}},
        output_json=None,
        status="running",
        visibility="user",
        created_at=utc_now(),
    )
    db_session.add(step1)
    await db_session.flush()
    call = await _seed_unknown_call(db_session, user.id, run, step1)
    wallet = await WalletService(db_session).get_wallet(user.id)
    assert (wallet.balance, wallet.reserved) == (990, 10)

    _expire_lease(await db_session.get(AgentRun, run.id))
    await db_session.flush()

    # 模型重发相同调用（同工具同参数），随后 complete。
    gateway = FakeAgentGateway(
        [
            CallTool(
                action="call_tool",
                internal_tool_name=INTERNAL_NAME,
                arguments={"keyword": "美妆"},
                rationale="重查美妆数据",
            ),
            Complete(action="complete", text="结果待核对，先给受限结论"),
        ]
    )
    executor = _build_executor(
        db_session,
        gateway=gateway,
        registry=_mcp_registry(db_session, FakeMcpTransport([])),
    )

    run_id = await executor.claim_and_process_one()

    assert run_id == run.id
    assert (await db_session.get(AgentRun, run.id)).status == RunStatus.COMPLETED
    # 第一轮 decide 的 transcript 已回放 unknown 结果（不依赖模型记忆防重）
    first_messages = gateway.calls[0]["messages"]
    assert any(
        message.role == "user" and "result_unknown" in message.content
        for message in first_messages
    )
    # 复用原 Step：全 Run 只有一个 tool_call Step，且归属到新 Attempt
    steps = await _tool_steps(db_session, run.id)
    assert len(steps) == 1
    assert steps[0].id == step1.id
    attempts = list(
        (
            await db_session.scalars(
                select(AgentRunAttempt)
                .where(AgentRunAttempt.run_id == run.id)
                .order_by(AgentRunAttempt.attempt)
            )
        ).all()
    )
    assert [attempt.attempt for attempt in attempts] == [1, 2]
    assert steps[0].attempt_id == attempts[1].id
    # 零新外发、零新预留：logical_call_id 幂等回放 unknown 行
    fresh_call = await db_session.get(AgentToolCall, call.id)
    assert fresh_call.status == "unknown"
    assert fresh_call.points_reserved == 10
    count = await db_session.scalar(
        select(func.count(AgentToolCall.id)).where(AgentToolCall.run_id == run.id)
    )
    assert count == 1
    wallet = await WalletService(db_session).get_wallet(user.id)
    assert (wallet.balance, wallet.reserved) == (990, 10)


async def test_takeover_after_settle_before_step_update_replays_success(
    db_session, user_factory
) -> None:
    """崩溃现场：调用已 settled（Evidence 已写）但 Step 仍是 running（settle 后、
    Step 更新前崩溃）。接管后 transcript 从调用行回放 success，模型不再重复调用。
    """
    run, session, user = await _make_session(db_session, user_factory)
    await WalletService(db_session).ensure_welcome_grant(user.id)
    attempt1 = await _start_attempt(db_session, run, worker="worker-a")
    transport_a = FakeMcpTransport([_ok_result()])
    registry_a = _mcp_registry(db_session, transport_a)
    step1 = AgentStep(
        id=str(uuid4()),
        run_id=run.id,
        attempt_id=attempt1.id,
        sequence=2,
        step_type="tool_call",
        input_json={"internal_tool_name": INTERNAL_NAME, "arguments": {"keyword": "美妆"}},
        output_json=None,
        status="running",
        visibility="user",
        created_at=utc_now(),
    )
    db_session.add(step1)
    await db_session.flush()
    result = await registry_a.execute(
        internal_name=INTERNAL_NAME,
        arguments={"keyword": "美妆"},
        user_id=user.id,
        session_id=session.id,
        run_id=run.id,
        profile=get_profile("session_analyst_v1"),
        step_id=step1.id,
    )
    # 崩溃注入：Step 保持 running（引擎还没来得及写 output），租约过期。
    _expire_lease(await db_session.get(AgentRun, run.id))
    await db_session.flush()

    gateway = FakeAgentGateway([Complete(action="complete", text="数据已齐，给结论")])
    executor = _build_executor(
        db_session,
        gateway=gateway,
        registry=_mcp_registry(db_session, FakeMcpTransport([])),
    )

    run_id = await executor.claim_and_process_one()

    assert run_id == run.id
    assert (await db_session.get(AgentRun, run.id)).status == RunStatus.COMPLETED
    # Step 虽残留 running，transcript 仍从已 settled 调用行回放 success
    tool_results = _tool_results(gateway.calls[0]["messages"])
    assert len(tool_results) == 1
    assert tool_results[0]["status"] == "success"
    assert tool_results[0]["evidence_id"] == result.evidence_id
    # 模型看到结果直接 complete：零新外发、零新扣费
    assert (await WalletService(db_session).get_wallet(user.id)).balance == 990


async def test_takeover_after_decide_crash_resumes_from_trigger_message(
    db_session, user_factory
) -> None:
    """崩溃现场：decide 后崩溃（running model_decision 残留，无工具历史）。

    接管后 transcript 只有触发消息，模型重新 decide 正常推进。
    """
    run, _, _ = await _make_session(db_session, user_factory)
    attempt1 = await _start_attempt(db_session, run, worker="worker-a")
    db_session.add(
        AgentStep(
            id=str(uuid4()),
            run_id=run.id,
            attempt_id=attempt1.id,
            sequence=1,
            step_type="model_decision",
            input_json=[{"role": "user", "content": "帮我分析品牌"}],
            output_json=None,
            status="running",
            visibility="user",
            created_at=utc_now(),
        )
    )
    _expire_lease(await db_session.get(AgentRun, run.id))
    await db_session.flush()

    gateway = FakeAgentGateway([Complete(action="complete", text="分析完成")])
    executor = _build_executor(db_session, gateway=gateway)

    run_id = await executor.claim_and_process_one()

    assert run_id == run.id
    assert (await db_session.get(AgentRun, run.id)).status == RunStatus.COMPLETED
    first_messages = gateway.calls[0]["messages"]
    assert not any("tool_result" in message.content for message in first_messages)


async def test_user_resume_marks_resumed_by_user(db_session, user_factory) -> None:
    """用户主动 resume（API begin_attempt(resumed=True) 留下的 NULL 租约 running）：
    run.resumed 事件带 resumed_by="user"，与系统接管可区分。
    """
    run, _, _ = await _make_session(db_session, user_factory)
    repo = AgentRunRepository(db_session)
    await repo.begin_attempt(run.id)
    await repo.claim_lease(run.id, "worker-a", 300)
    assert await repo.pause(run.id, "worker-a")
    # 用户主动恢复：新建 Attempt，Run running 但无租约（API resume 现场）。
    await repo.begin_attempt(run.id, resumed=True)
    await db_session.flush()

    gateway = FakeAgentGateway([Complete(action="complete", text="恢复完成")])
    executor = _build_executor(db_session, gateway=gateway)

    run_id = await executor.claim_and_process_one()

    assert run_id == run.id
    resumed = [event for event in await _events(db_session, run.id) if event.event_type == "run.resumed"]
    assert len(resumed) == 1
    assert resumed[0].payload_json["resumed_by"] == "user"


# ---------------------------------------------------------------------------
# 3. 历史 reviewing Run 收口（直接发布改造：Reviewer 已下线，不再继续复核）
# ---------------------------------------------------------------------------


async def _make_legacy_reviewing_scene(
    db_session,
    run: AgentRun,
    *,
    brands: tuple[str, ...] = ("瑞幸", "蜜雪冰城"),
) -> list[ArtifactDraft]:
    """部署前复核期间崩溃的历史现场：run reviewing + Draft 仍由该 Run 持有。"""
    service = ArtifactService(db_session)
    drafts: list[ArtifactDraft] = []
    for brand in brands:
        _, draft, _ = await service.create_or_get_draft(
            session_id=run.session_id,
            user_id=run.user_id,
            run_id=run.id,
            module="insight",
            business_fields={"parent_artifact_version_id": "pv-1", "question": brand},
            schema_version="insight_board_v1",
            payload=insight_payload(title=brand),
            artifact_type="insight_board_v1",
        )
        draft.status = "reviewing"
        drafts.append(draft)
    row = await db_session.get(AgentRun, run.id)
    row.status = RunStatus.REVIEWING
    await db_session.flush()
    return drafts


async def test_legacy_reviewing_takeover_settles_failed_and_keeps_drafts(
    db_session, user_factory
) -> None:
    """历史 reviewing Run（部署前复核期间崩溃、租约过期）：恢复扫描收口 failed
    （error_code=LEGACY_REVIEWING_UNSUPPORTED），保留 Draft，不再启动 Reviewer。
    """
    run, _, _ = await _make_session(db_session, user_factory)
    await _start_attempt(db_session, run, worker="worker-a")
    drafts = await _make_legacy_reviewing_scene(db_session, run)
    _expire_lease(await db_session.get(AgentRun, run.id))
    await db_session.flush()

    gateway = FakeAgentGateway([])
    executor = _build_executor(db_session, gateway=gateway)
    recovery = _build_recovery(db_session, executor=executor)

    reclaimed = await recovery.reclaim_expired_runs()

    assert run.id in reclaimed
    # 不启动模型/Reviewer：零次 decide
    assert gateway.calls == []
    fresh = await db_session.get(AgentRun, run.id)
    assert fresh.status == RunStatus.FAILED
    assert fresh.error_code == "LEGACY_REVIEWING_UNSUPPORTED"
    # Draft 保留：owner 与 reviewing 状态不变
    for draft in drafts:
        fresh_draft = await db_session.get(ArtifactDraft, draft.id)
        assert fresh_draft.owner_run_id == run.id
        assert fresh_draft.status == "reviewing"
    # 不产生任何 Version；恰好一个 run.failed 终态事件
    versions = (
        await db_session.scalars(
            select(AgentArtifactVersion).where(
                AgentArtifactVersion.source_run_id == run.id
            )
        )
    ).all()
    assert versions == []
    events = await _events(db_session, run.id)
    terminal = [
        event
        for event in events
        if event.event_type
        in ("run.completed", "run.completed_with_warnings", "run.failed", "run.cancelled")
    ]
    assert len(terminal) == 1
    assert terminal[0].event_type == "run.failed"
    assert terminal[0].payload_json["error_code"] == "LEGACY_REVIEWING_UNSUPPORTED"


async def test_reviewing_run_with_unexpired_lease_is_not_reclaimed(
    db_session, user_factory
) -> None:
    """reviewing 且租约未过期：恢复循环与执行器都不得接管。"""
    run, _, _ = await _make_session(db_session, user_factory)
    await _start_attempt(db_session, run, worker="worker-a")
    await _make_legacy_reviewing_scene(db_session, run)
    # 租约仍有效（_start_attempt 已 claim 300s）

    gateway = FakeAgentGateway([])
    executor = _build_executor(db_session, gateway=gateway)
    recovery = _build_recovery(db_session, executor=executor)

    assert await executor.claim_and_process_one() is None
    reclaimed = await recovery.reclaim_expired_runs()
    assert run.id not in reclaimed
    assert (await db_session.get(AgentRun, run.id)).status == RunStatus.REVIEWING


# ---------------------------------------------------------------------------
# 4. 租约心跳：覆盖长 decide；租约被接管后旧 worker 不写终态
# ---------------------------------------------------------------------------


async def test_heartbeat_covers_long_decide_and_run_completes(
    db_session, user_factory
) -> None:
    """单次 decide 超过 lease_seconds：心跳每 lease/3 续租，Run 正常完成。

    无心跳时 decide 返回后 transition 必抛 run_lease_not_held（租约已过期）。
    """
    run, _, _ = await _make_session(db_session, user_factory)
    repo = AgentRunRepository(db_session)
    attempt = await repo.begin_attempt(run.id)
    await repo.claim_lease(run.id, "worker", 1)

    started = asyncio.Event()
    release = asyncio.Event()
    gateway = BlockingGateway(
        [Complete(action="complete", text="长调用完成")], started, release
    )
    broker = AgentEventBroker()
    events = AgentEventStream(db_session, broker)
    engine = AgentEngine(
        db_session,
        gateway=gateway,
        registry=ToolRegistry(),
        events=events,
        worker_id="worker",
        lease_seconds=1,  # 心跳间隔 = 1/3 秒
    )

    from app.model.contracts import ChatMessage

    task = asyncio.create_task(
        engine.run(
            run=run,
            attempt_id=attempt.id,
            profile=get_profile("session_analyst_v1"),
            messages=[ChatMessage(role="user", content="帮我分析品牌")],
        )
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    # decide 阻塞 1.2s（> lease 1s）：心跳应在此期间多次续租
    await asyncio.sleep(1.2)
    release.set()
    outcome = await asyncio.wait_for(task, timeout=5)

    assert outcome.status == RunStatus.COMPLETED
    fresh = await db_session.get(AgentRun, run.id)
    assert fresh.status == RunStatus.COMPLETED


async def test_old_worker_stops_after_lease_takeover_without_terminal_write(
    db_session, user_factory
) -> None:
    """decide 期间租约被其他 worker 接管：旧 worker 安静退出——不写终态、
    不发事件、不落 assistant 消息、不抢回租约。
    """
    run, _, _ = await _make_session(db_session, user_factory)
    repo = AgentRunRepository(db_session)
    attempt = await repo.begin_attempt(run.id)
    await repo.claim_lease(run.id, "worker", 300)

    started = asyncio.Event()
    release = asyncio.Event()
    gateway = BlockingGateway(
        [Complete(action="complete", text="不应落库")], started, release
    )
    broker = AgentEventBroker()
    events = AgentEventStream(db_session, broker)
    engine = AgentEngine(
        db_session,
        gateway=gateway,
        registry=ToolRegistry(),
        events=events,
        worker_id="worker",
        lease_seconds=300,
    )

    from app.model.contracts import ChatMessage

    task = asyncio.create_task(
        engine.run(
            run=run,
            attempt_id=attempt.id,
            profile=get_profile("session_analyst_v1"),
            messages=[ChatMessage(role="user", content="帮我分析品牌")],
        )
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    # 模拟另一 worker 接管租约
    row = await db_session.get(AgentRun, run.id)
    row.lease_owner = "worker-b"
    row.lease_expires_at = utc_now() + timedelta(seconds=300)
    await db_session.flush()
    release.set()
    await asyncio.wait_for(task, timeout=5)

    fresh = await db_session.get(AgentRun, run.id)
    # 旧 worker 未写任何终态；租约仍归接管方
    assert fresh.status == RunStatus.RUNNING
    assert fresh.lease_owner == "worker-b"
    assert fresh.completed_at is None
    event_types = [event.event_type for event in await _events(db_session, run.id)]
    assert "run.completed" not in event_types
    assert "run.failed" not in event_types
    assert "run.cancelled" not in event_types
    assistant_count = await db_session.scalar(
        select(func.count(AgentMessage.id)).where(
            AgentMessage.run_id == run.id, AgentMessage.role == "assistant"
        )
    )
    assert assistant_count == 0


async def test_publish_is_skipped_when_lease_lost_before_dispatch(
    db_session, user_factory
) -> None:
    """decide 返回 publish_artifacts 后租约被接管：分发前闸门拦截——旧 worker
    不发布 Artifact、不发 artifact 事件、不写终态，安静交还接管方（§5.5）。
    """
    run, _, _ = await _make_session(db_session, user_factory)
    repo = AgentRunRepository(db_session)
    attempt = await repo.begin_attempt(run.id)
    await repo.claim_lease(run.id, "worker", 300)
    service = ArtifactService(db_session)
    _, draft, _ = await service.create_or_get_draft(
        session_id=run.session_id,
        user_id=run.user_id,
        run_id=run.id,
        module="insight",
        business_fields={"parent_artifact_version_id": "pv-1", "question": "瑞幸"},
        schema_version="insight_board_v1",
        payload=PAYLOAD_V1,
        artifact_type="insight_board_v1",
    )

    async def steal_lease(_run, _index) -> None:
        row = await db_session.get(AgentRun, run.id)
        row.lease_owner = "worker-b"
        row.lease_expires_at = utc_now() + timedelta(seconds=300)
        await db_session.flush()

    gateway = FakeAgentGateway(
        [
            PublishArtifacts(
                action="publish_artifacts",
                artifact_draft_ids=(draft.id,),
                summary="瑞幸品牌分析",
            )
        ],
        on_decide=steal_lease,
    )
    broker = AgentEventBroker()
    events = AgentEventStream(db_session, broker)
    engine = AgentEngine(
        db_session,
        gateway=gateway,
        registry=ToolRegistry(),
        events=events,
        worker_id="worker",
        lease_seconds=300,
    )

    from app.model.contracts import ChatMessage

    await engine.run(
        run=run,
        attempt_id=attempt.id,
        profile=get_profile("session_analyst_v1"),
        messages=[ChatMessage(role="user", content="分析瑞幸品牌")],
    )

    # 未发布、未写终态；Run 保持 running 等待接管方恢复
    assert (
        await db_session.scalar(
            select(func.count(AgentArtifactVersion.id)).where(
                AgentArtifactVersion.source_run_id == run.id
            )
        )
    ) == 0
    fresh = await db_session.get(AgentRun, run.id)
    assert fresh.status == RunStatus.RUNNING
    assert fresh.lease_owner == "worker-b"
    event_types = [event.event_type for event in await _events(db_session, run.id)]
    assert "artifact.published" not in event_types
    assert "artifact.publish.completed" not in event_types
    assert "run.completed" not in event_types
    assert "run.failed" not in event_types


# ---------------------------------------------------------------------------
# 5. 取消语义：decide 后 / publish 后收口，恰好一个 run.cancelled
# ---------------------------------------------------------------------------


def _make_engine(
    db_session,
    *,
    gateway: Any,
    registry: ToolRegistry | None = None,
    worker: str = "worker",
) -> AgentEngine:
    broker = AgentEventBroker()
    events = AgentEventStream(db_session, broker)
    return AgentEngine(
        db_session,
        gateway=gateway,
        registry=registry or ToolRegistry(),
        events=events,
        worker_id=worker,
    )


async def test_cancel_after_decide_suppresses_assistant_message_on_complete(
    db_session, user_factory
) -> None:
    """取消与 decide 竞态（minor b）：decide 返回 complete 时取消已到达——
    不得落 assistant 消息，收口为恰好一个 run.cancelled 事件。
    """
    run, _, _ = await _make_session(db_session, user_factory)
    repo = AgentRunRepository(db_session)
    attempt = await repo.begin_attempt(run.id)
    await repo.claim_lease(run.id, "worker", 300)

    async def cancel_on_first_decide(_run, _index) -> None:
        await repo.request_cancel(run.id, run.user_id)

    gateway = FakeAgentGateway(
        [Complete(action="complete", text="不应落库的回复")],
        on_decide=cancel_on_first_decide,
    )
    engine = _make_engine(db_session, gateway=gateway)

    from app.model.contracts import ChatMessage

    outcome = await engine.run(
        run=run,
        attempt_id=attempt.id,
        profile=get_profile("session_analyst_v1"),
        messages=[ChatMessage(role="user", content="帮我分析品牌")],
    )

    assert outcome.status == RunStatus.CANCELLED
    assistant_count = await db_session.scalar(
        select(func.count(AgentMessage.id)).where(
            AgentMessage.run_id == run.id, AgentMessage.role == "assistant"
        )
    )
    assert assistant_count == 0
    event_types = [event.event_type for event in await _events(db_session, run.id)]
    assert event_types.count("run.cancelled") == 1
    assert "message.completed" not in event_types
    assert "run.completed" not in event_types


async def test_cancel_after_decide_suppresses_assistant_message_on_ask_user(
    db_session, user_factory
) -> None:
    """decide 返回 ask_user 时取消已到达：同样不得落澄清消息与 pending Memory。"""
    run, _, _ = await _make_session(db_session, user_factory)
    repo = AgentRunRepository(db_session)
    attempt = await repo.begin_attempt(run.id)
    await repo.claim_lease(run.id, "worker", 300)

    async def cancel_on_first_decide(_run, _index) -> None:
        await repo.request_cancel(run.id, run.user_id)

    gateway = FakeAgentGateway(
        [AskUser(action="ask_user", question="确认品牌？", options=["瑞幸", "蜜雪冰城"])],
        on_decide=cancel_on_first_decide,
    )
    engine = _make_engine(db_session, gateway=gateway)

    from app.model.contracts import ChatMessage

    outcome = await engine.run(
        run=run,
        attempt_id=attempt.id,
        profile=get_profile("session_analyst_v1"),
        messages=[ChatMessage(role="user", content="帮我分析品牌")],
    )

    assert outcome.status == RunStatus.CANCELLED
    assistant_count = await db_session.scalar(
        select(func.count(AgentMessage.id)).where(
            AgentMessage.run_id == run.id, AgentMessage.role == "assistant"
        )
    )
    assert assistant_count == 0
    event_types = [event.event_type for event in await _events(db_session, run.id)]
    assert event_types.count("run.cancelled") == 1


async def test_cancel_after_publish_suppresses_complete_and_cancels(
    db_session, user_factory
) -> None:
    """publish_artifacts 发布后、complete 分发前收到取消：不落 assistant 消息、
    不写 run.completed；已发布 Artifact 保留，收口恰好一个 run.cancelled。
    """
    run, _, _ = await _make_session(db_session, user_factory)
    repo = AgentRunRepository(db_session)
    attempt = await repo.begin_attempt(run.id)
    await repo.claim_lease(run.id, "worker", 300)
    service = ArtifactService(db_session)
    _, draft, _ = await service.create_or_get_draft(
        session_id=run.session_id,
        user_id=run.user_id,
        run_id=run.id,
        module="insight",
        business_fields={"parent_artifact_version_id": "pv-1", "question": "瑞幸"},
        schema_version="insight_board_v1",
        payload=PAYLOAD_V1,
        artifact_type="insight_board_v1",
    )

    async def cancel_on_second_decide(_run, index) -> None:
        if index == 2:
            await repo.request_cancel(run.id, run.user_id)

    gateway = FakeAgentGateway(
        [
            PublishArtifacts(
                action="publish_artifacts",
                artifact_draft_ids=(draft.id,),
                summary="瑞幸品牌分析",
            ),
            Complete(action="complete", text="不应落库的回复"),
        ],
        on_decide=cancel_on_second_decide,
    )
    engine = _make_engine(db_session, gateway=gateway)

    from app.model.contracts import ChatMessage

    outcome = await engine.run(
        run=run,
        attempt_id=attempt.id,
        profile=get_profile("session_analyst_v1"),
        messages=[ChatMessage(role="user", content="分析瑞幸品牌")],
    )

    assert outcome.status == RunStatus.CANCELLED
    # 取消前已完成的发布不回滚（发布是逐项独立提交的）
    assert (
        await db_session.scalar(
            select(func.count(AgentArtifactVersion.id)).where(
                AgentArtifactVersion.source_run_id == run.id
            )
        )
    ) == 1
    # complete 被分发前闸门拦截：不落 assistant 消息
    assistant_count = await db_session.scalar(
        select(func.count(AgentMessage.id)).where(
            AgentMessage.run_id == run.id, AgentMessage.role == "assistant"
        )
    )
    assert assistant_count == 0
    event_types = [event.event_type for event in await _events(db_session, run.id)]
    assert event_types.count("run.cancelled") == 1
    assert "run.completed" not in event_types
    assert "message.completed" not in event_types
