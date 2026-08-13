"""Pi POC Runner 的 Run/Attempt 生命周期测试（SQLite 内存库）。"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

import app.db.models  # noqa: F401
from app.agent_artifacts.models import AgentArtifact, AgentArtifactVersion
from app.agent_runtime.events import AgentEventBroker, AgentEventStream
from app.agent_runtime.models import (
    AgentEvent,
    AgentMessage,
    AgentRun,
    AgentRunAttempt,
    AgentSession,
    AgentStep,
)
from app.agent_runtime.repository import AgentRunRepository
from app.agent_runtime.state import RunStatus
from app.core.config import Settings
from app.identity.models import User
from app.marketing_capability_pack.runtime import build_marketing_run_capability
from app.pi_runtime_poc.rpc import PiRpcProtocolError
from app.pi_runtime_poc.runner import PiPocRunner


@compiles(MEDIUMTEXT, "sqlite")
def _mediumtext_sqlite(element: Any, compiler: Any, **kw: Any) -> str:
    return "TEXT"


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class FakePiClient:
    def __init__(self, records: list[dict[str, Any]] | None = None, error: Exception | None = None) -> None:
        self.records = records or []
        self.error = error
        self.prompts: list[str] = []
        self.aborted = False
        self.closed = False

    async def prompt(self, message: str) -> str:
        self.prompts.append(message)
        return "prompt-1"

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        for record in self.records:
            yield record
        if self.error is not None:
            raise self.error

    async def abort(self) -> None:
        self.aborted = True

    async def close(self) -> None:
        self.closed = True


@pytest_asyncio.fixture
async def db() -> AsyncSession:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    from app.db.base import Base

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def queued_run(db: AsyncSession) -> AgentRun:
    now = _now()
    user = User(id=str(uuid4()), nickname="poc", role="user", status="active", created_at=now, updated_at=now)
    session = AgentSession(
        id=str(uuid4()), user_id=user.id, title="poc", status="active", summary_version=0,
        created_at=now, updated_at=now,
    )
    run = AgentRun(
        id=str(uuid4()), session_id=session.id, user_id=user.id, run_kind="user", visibility="user",
        profile_name="pi_poc", profile_version="v1", model="test", status="queued",
        prompt_snapshot_json={},
        runtime_config_snapshot_json={
            "capability_pack": build_marketing_run_capability(
                model_version="test"
            ).model_dump(mode="json")
        },
    )
    message = AgentMessage(
        id=str(uuid4()), session_id=session.id, run_id=run.id, role="user", content="请解释现有报告",
        sequence=1, created_at=now,
    )
    run.input_message_id = message.id
    db.add_all([user, session, run, message])
    await db.commit()
    return run


def make_runner(db: AsyncSession, settings: Settings, client: FakePiClient) -> PiPocRunner:
    return PiPocRunner(
        db=db,
        events=AgentEventStream(db, AgentEventBroker()),
        settings=settings,
        worker_id="pi-poc-test",
        client_factory=lambda _run, _token: client,
    )


async def test_runner_completes_non_marketing_reply_with_one_terminal_event(
    db: AsyncSession, settings: Settings, queued_run: AgentRun
) -> None:
    client = FakePiClient(
        [
            {"type": "response", "command": "prompt", "success": True},
            {"type": "agent_start"},
            {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "仅处理社媒营销。"}},
            {"type": "turn_end"},
            {"type": "agent_end", "willRetry": False, "messageCount": 1},
        ]
    )

    outcome = await make_runner(db, settings, client).run(queued_run.id)

    assert outcome == "completed"
    assert client.closed
    assert client.prompts
    assert "[MARKETING_ROOT_POLICY]" not in client.prompts[0]
    assert "非营销主题必须使用固定范围回复" not in client.prompts[0]
    events = (await db.scalars(select(AgentEvent).where(AgentEvent.run_id == queued_run.id))).all()
    assert [event.event_type for event in events].count("run.completed") == 1
    assert any(event.event_type == "message.completed" for event in events)


async def test_runner_cancels_after_persisted_cancel_request(
    db: AsyncSession, settings: Settings, queued_run: AgentRun
) -> None:
    client = FakePiClient([{"type": "agent_start"}, {"type": "agent_end", "willRetry": False}])
    queued_run.cancel_requested = True
    await db.commit()

    outcome = await make_runner(db, settings, client).run(queued_run.id)

    assert outcome == "cancelled"
    # 已持久化取消的 queued Run 不得启动 Pi 子进程，因此无需发送 abort RPC。
    assert not client.aborted
    assert not client.prompts
    event = await db.scalar(
        select(AgentEvent).where(AgentEvent.run_id == queued_run.id, AgentEvent.event_type == "run.cancelled")
    )
    assert event is not None


async def test_runner_marks_rpc_crash_failed(
    db: AsyncSession, settings: Settings, queued_run: AgentRun
) -> None:
    client = FakePiClient(error=RuntimeError("rpc disconnected"))

    outcome = await make_runner(db, settings, client).run(queued_run.id)

    assert outcome == "failed"
    run = await db.get(AgentRun, queued_run.id)
    assert run is not None and run.status == "failed"
    assert client.closed


async def test_runner_records_safe_rpc_protocol_subcode(
    db: AsyncSession, settings: Settings, queued_run: AgentRun
) -> None:
    """协议边界的稳定分类必须落入 Run 终态，不能泄漏原始记录或退化为异常类名。"""
    client = FakePiClient(error=PiRpcProtocolError("pi_rpc_record_too_large"))

    outcome = await make_runner(db, settings, client).run(queued_run.id)

    assert outcome == "failed"
    failed = await db.scalar(
        select(AgentEvent).where(
            AgentEvent.run_id == queued_run.id,
            AgentEvent.event_type == "run.failed",
        )
    )
    assert failed is not None
    assert failed.payload_json["error_code"] == "pi_rpc_record_too_large"


async def test_runner_closes_queued_run_when_extension_bootstrap_fails(
    db: AsyncSession, settings: Settings, queued_run: AgentRun
) -> None:
    def failing_factory(_run: AgentRun, _token: str) -> FakePiClient:
        raise RuntimeError("fake_extension_loader_failure")

    runner = PiPocRunner(
        db=db,
        events=AgentEventStream(db, AgentEventBroker()),
        settings=settings,
        worker_id="pi-poc-test",
        client_factory=failing_factory,
    )

    outcome = await runner.run(queued_run.id)

    assert outcome == "failed"
    run = await db.get(AgentRun, queued_run.id)
    assert run is not None and run.status == "failed"
    failed = await db.scalar(
        select(AgentEvent).where(
            AgentEvent.run_id == queued_run.id,
            AgentEvent.event_type == "run.failed",
        )
    )
    assert failed is not None


async def test_runner_completes_on_real_pi_agent_end_without_retry(
    db: AsyncSession, settings: Settings, queued_run: AgentRun
) -> None:
    client = FakePiClient(
        [
            {"type": "response", "command": "prompt", "success": True},
            {"type": "agent_start"},
            {"type": "turn_end"},
            {"type": "agent_end", "willRetry": False, "messageCount": 0},
        ]
    )

    outcome = await make_runner(db, settings, client).run(queued_run.id)

    assert outcome == "completed_with_warnings"
    event = await db.scalar(
        select(AgentEvent).where(
            AgentEvent.run_id == queued_run.id,
            AgentEvent.event_type == "run.completed_with_warnings",
        )
    )
    assert event is not None


async def test_runner_waits_past_retrying_agent_end_for_final_agent_end(
    db: AsyncSession, settings: Settings, queued_run: AgentRun
) -> None:
    """willRetry=true 不是终态；必须等待下一轮 agent_end(false)。"""
    client = FakePiClient(
        [
            {"type": "response", "command": "prompt", "success": True},
            {"type": "agent_start"},
            {"type": "turn_end"},
            {"type": "agent_end", "willRetry": True, "messageCount": 1},
            {"type": "agent_start"},
            {"type": "turn_end"},
            {"type": "agent_end", "willRetry": False, "messageCount": 2},
        ]
    )

    outcome = await make_runner(db, settings, client).run(queued_run.id)

    assert outcome == "completed_with_warnings"
    attempt = await db.scalar(select(AgentRunAttempt).where(AgentRunAttempt.run_id == queued_run.id))
    assert attempt is not None and attempt.decision_count == 2


async def test_runner_projects_raw_message_snapshots_before_step_and_product_event(
    db: AsyncSession, settings: Settings, queued_run: AgentRun
) -> None:
    """Runner 防御性投影：直接注入的原始 Pi 事件也不能污染审计或产品事件。"""
    client = FakePiClient(
        [
            {"type": "agent_start"},
            {
                "type": "message_update",
                "message": {"content": "message-snapshot-must-not-persist"},
                "assistantMessageEvent": {
                    "type": "text_delta",
                    "delta": "安全增量",
                    "partial": {"content": "partial-snapshot-must-not-persist"},
                },
            },
            {
                "type": "agent_end",
                "willRetry": False,
                "messages": [{"content": "terminal-messages-must-not-persist"}],
            },
        ]
    )

    outcome = await make_runner(db, settings, client).run(queued_run.id)

    assert outcome == "completed"
    steps = (
        await db.scalars(
            select(AgentStep).where(
                AgentStep.run_id == queued_run.id,
                AgentStep.step_type == "pi_rpc_event",
            )
        )
    ).all()
    product_events = (await db.scalars(select(AgentEvent).where(AgentEvent.run_id == queued_run.id))).all()
    persisted = "\n".join(str(step.input_json) for step in steps) + "\n" + "\n".join(
        str(event.payload_json) for event in product_events
    )
    assert "message-snapshot-must-not-persist" not in persisted
    assert "partial-snapshot-must-not-persist" not in persisted
    assert "terminal-messages-must-not-persist" not in persisted
    terminal_step = next(step for step in steps if step.input_json["event"]["type"] == "agent_end")
    assert terminal_step.input_json == {
        "event": {"type": "agent_end", "willRetry": False, "messageCount": 1}
    }


async def test_runner_flushes_thinking_before_natural_eof(
    db: AsyncSession, settings: Settings, queued_run: AgentRun
) -> None:
    client = FakePiClient(
        [
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "thinking_delta", "delta": "EOF 前的思考"},
            }
        ]
    )

    outcome = await make_runner(db, settings, client).run(queued_run.id)

    assert outcome == "failed"
    chunks = list(
        (
            await db.scalars(
                select(AgentStep.thinking_text).where(
                    AgentStep.run_id == queued_run.id,
                    AgentStep.step_type == "pi_rpc_thinking_chunk",
                )
            )
        ).all()
    )
    assert chunks == ["EOF 前的思考"]


async def test_runner_flushes_thinking_before_rpc_exception(
    db: AsyncSession, settings: Settings, queued_run: AgentRun
) -> None:
    client = FakePiClient(
        [
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "thinking_delta", "delta": "异常前的思考"},
            }
        ],
        error=RuntimeError("fake rpc disconnect"),
    )

    outcome = await make_runner(db, settings, client).run(queued_run.id)

    assert outcome == "failed"
    chunks = list(
        (
            await db.scalars(
                select(AgentStep.thinking_text).where(
                    AgentStep.run_id == queued_run.id,
                    AgentStep.step_type == "pi_rpc_thinking_chunk",
                )
            )
        ).all()
    )
    assert chunks == ["异常前的思考"]


async def test_runner_batches_thinking_deltas_without_reordering_text(
    db: AsyncSession, settings: Settings, queued_run: AgentRun
) -> None:
    deltas = [
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "thinking_delta", "delta": "x"},
        }
        for _ in range(2048)
    ]
    client = FakePiClient(
        [{"type": "agent_start"}, *deltas, {"type": "agent_end", "willRetry": False}]
    )

    outcome = await make_runner(db, settings, client).run(queued_run.id)

    assert outcome == "completed_with_warnings"
    steps = (
        await db.scalars(
            select(AgentStep).where(
                AgentStep.run_id == queued_run.id,
                AgentStep.step_type == "pi_rpc_thinking_chunk",
            )
        )
    ).all()
    events = (
        await db.scalars(
            select(AgentEvent)
            .where(
                AgentEvent.run_id == queued_run.id,
                AgentEvent.event_type == "thinking.delta",
            )
            .order_by(AgentEvent.sequence)
        )
    ).all()

    assert len(steps) <= 2
    assert len(events) <= 2
    assert "".join(str(event.payload_json["text"]) for event in events) == "x" * 2048


async def test_runner_stops_at_controlled_clarification_without_terminal_or_artifact(
    db: AsyncSession, settings: Settings, queued_run: AgentRun
) -> None:
    class ClarifyingClient(FakePiClient):
        async def events(self) -> AsyncIterator[dict[str, Any]]:
            yield {"type": "agent_start"}
            await AgentRunRepository(db).transition(
                queued_run.id,
                RunStatus.CLARIFICATION_REQUESTED,
                worker_id="pi-poc-test",
            )
            await db.commit()
            yield {
                "type": "tool_execution_end",
                "toolCallId": "clarify-1",
                "toolName": "request_clarification",
                "isError": False,
            }

    client = ClarifyingClient()
    outcome = await make_runner(db, settings, client).run(queued_run.id)

    assert outcome == "clarification_requested"
    assert client.aborted
    events = (await db.scalars(select(AgentEvent).where(AgentEvent.run_id == queued_run.id))).all()
    assert any(event.event_type == "tool.succeeded" for event in events)
    assert not any(event.event_type.startswith("run.") and event.event_type != "run.started" for event in events)
    assert not any(event.event_type == "artifact.published" for event in events)


async def test_runner_fails_on_pi_error_record_without_leaking_error_text(
    db: AsyncSession, settings: Settings, queued_run: AgentRun
) -> None:
    client = FakePiClient(
        [{"type": "error", "message": "untrusted-error-details"}]
    )

    outcome = await make_runner(db, settings, client).run(queued_run.id)

    assert outcome == "failed"
    events = (await db.scalars(select(AgentEvent).where(AgentEvent.run_id == queued_run.id))).all()
    error_event = next(event for event in events if event.event_type == "thinking.failed")
    assert error_event.payload_json == {
        "code": "pi_rpc_error",
        "collapsed": True,
        "run_id": queued_run.id,
    }
    assert all("untrusted-error-details" not in str(event.payload_json) for event in events)


async def test_runner_emits_only_persisted_published_artifact_and_marks_partial_warning(
    db: AsyncSession, settings: Settings, queued_run: AgentRun
) -> None:
    now = _now()
    artifact = AgentArtifact(
        id=str(uuid4()),
        session_id=queued_run.session_id,
        user_id=queued_run.user_id,
        module="brand",
        artifact_type="brand_report",
        artifact_key="pi-poc-brand",
        status="published",
        latest_version=1,
        activity_sequence=1,
        created_at=now,
        updated_at=now,
    )
    version = AgentArtifactVersion(
        id=str(uuid4()),
        artifact_id=artifact.id,
        version=1,
        source_run_id=queued_run.id,
        source_draft_revision_id=str(uuid4()),
        schema_version="brand_report_v3",
        payload_json={"data_status": "partial"},
        evidence_refs_json=[],
        lineage_snapshot_json={},
        review_json={},
        validation_json={},
        data_status="partial",
        created_at=now,
    )
    db.add_all([artifact, version])
    await db.commit()
    client = FakePiClient(
        [
            {"type": "agent_start"},
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_delta", "delta": "数据受限，已披露限制。"},
            },
            {"type": "agent_end", "willRetry": False},
        ]
    )

    outcome = await make_runner(db, settings, client).run(queued_run.id)

    assert outcome == "completed_with_warnings"
    artifact_event = await db.scalar(
        select(AgentEvent).where(
            AgentEvent.run_id == queued_run.id,
            AgentEvent.event_type == "artifact.published",
        )
    )
    assert artifact_event is not None
    assert artifact_event.payload_json == {
        "artifact_id": artifact.id,
        "artifact_version_id": version.id,
        "version": 1,
        "module": "brand",
        "run_id": queued_run.id,
    }


async def test_runner_fails_and_aborts_after_fifty_decisions(
    db: AsyncSession, settings: Settings, queued_run: AgentRun
) -> None:
    client = FakePiClient([{"type": "agent_start"}] * 51)

    outcome = await make_runner(db, settings, client).run(queued_run.id)

    assert outcome == "failed"
    assert client.aborted
    event = await db.scalar(
        select(AgentEvent).where(AgentEvent.run_id == queued_run.id, AgentEvent.event_type == "run.failed")
    )
    assert event is not None and event.payload_json["error_code"] == "pi_decision_limit"


async def test_runner_fails_and_aborts_after_attempt_wall_clock_timeout(
    db: AsyncSession, settings: Settings, queued_run: AgentRun
) -> None:
    class TimeoutClient(FakePiClient):
        async def events(self) -> AsyncIterator[dict[str, Any]]:
            yield {"type": "agent_start"}
            attempt = await db.scalar(
                select(AgentRunAttempt).where(AgentRunAttempt.run_id == queued_run.id)
            )
            assert attempt is not None
            attempt.started_at = _now() - timedelta(
                seconds=settings.pi_runtime_poc_run_timeout_seconds + 1
            )
            await db.commit()
            yield {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "晚到"}}

    client = TimeoutClient()
    outcome = await make_runner(db, settings, client).run(queued_run.id)

    assert outcome == "failed"
    assert client.aborted
    event = await db.scalar(
        select(AgentEvent).where(AgentEvent.run_id == queued_run.id, AgentEvent.event_type == "run.failed")
    )
    assert event is not None and event.payload_json["error_code"] == "pi_run_timeout"


async def test_runner_flushes_thinking_before_timeout(
    db: AsyncSession, settings: Settings, queued_run: AgentRun
) -> None:
    class ThinkingTimeoutClient(FakePiClient):
        async def events(self) -> AsyncIterator[dict[str, Any]]:
            yield {
                "type": "message_update",
                "assistantMessageEvent": {"type": "thinking_delta", "delta": "超时前的思考"},
            }
            attempt = await db.scalar(
                select(AgentRunAttempt).where(AgentRunAttempt.run_id == queued_run.id)
            )
            assert attempt is not None
            attempt.started_at = _now() - timedelta(
                seconds=settings.pi_runtime_poc_run_timeout_seconds + 1
            )
            await db.commit()
            yield {"type": "agent_start"}

    client = ThinkingTimeoutClient()
    outcome = await make_runner(db, settings, client).run(queued_run.id)

    assert outcome == "failed"
    chunks = list(
        (
            await db.scalars(
                select(AgentStep.thinking_text).where(
                    AgentStep.run_id == queued_run.id,
                    AgentStep.step_type == "pi_rpc_thinking_chunk",
                )
            )
        ).all()
    )
    assert chunks == ["超时前的思考"]
