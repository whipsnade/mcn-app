"""Pi POC Evidence 旁路持久化测试（SQLite 内存库，绝不连接任何 MySQL 库）。

覆盖：错误 Run token 401、未知 Run/跨 Run call id 404、start 幂等、settle 幂等、
成功创建零积分 AgentStep + AgentToolCall + EvidenceItem、failed/unknown 不产生
available Evidence、事件 tool.started/succeeded/failed/unknown、原始 payload hash
可复核。
"""

import hashlib
import secrets
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool


# SQLite 方言下把 MySQL MEDIUMTEXT 编译为 TEXT（models 共用同一 Base.metadata）。
@compiles(MEDIUMTEXT, "sqlite")
def _mediumtext_sqlite(element: Any, compiler: Any, **kw: Any) -> str:
    return "TEXT"


from app.agent_runtime.events import AgentEventBroker, AgentEventStream
from app.agent_runtime.models import (
    AgentEvent,
    AgentRun,
    AgentRunAttempt,
    AgentSession,
    AgentStep,
    AgentToolCall,
    EvidenceItem,
)
from app.core.config import Settings
from app.db.base import Base
from app.identity.models import User
from app.mcp_gateway.validation import canonical_json_bytes
from app.pi_runtime_poc.auth import issue_run_token
from app.pi_runtime_poc.schemas import PiToolFailed, PiToolSettled, PiToolStarted
from app.pi_runtime_poc.service import PiEvidenceIngestService


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@pytest_asyncio.fixture
async def db() -> AsyncSession:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        mysql_database="kol_insight_pi_poc",
        mysql_password=SecretStr(secrets.token_urlsafe(24)),
        jwt_secret=SecretStr(secrets.token_urlsafe(32)),
        tencent_plan_api_key=SecretStr(secrets.token_urlsafe(24)),
        datatap_mcp_token=SecretStr(secrets.token_urlsafe(24)),
        pi_runtime_poc_enabled=True,
        pi_runtime_poc_internal_secret=SecretStr(secrets.token_urlsafe(32)),
    )


@pytest_asyncio.fixture
async def seeded(db: AsyncSession) -> dict[str, Any]:
    now = _now()
    user = User(id=str(uuid4()), nickname="poc", role="user", status="active", created_at=now, updated_at=now)
    db.add(user)
    await db.flush()
    session = AgentSession(
        id=str(uuid4()),
        user_id=user.id,
        title="poc",
        status="active",
        summary_version=0,
        created_at=now,
        updated_at=now,
    )
    db.add(session)
    await db.flush()
    run = AgentRun(
        id=str(uuid4()),
        session_id=session.id,
        user_id=user.id,
        run_kind="user",
        visibility="user",
        profile_name="session_analyst_v1",
        profile_version="1",
        model="test",
        status="running",
    )
    db.add(run)
    await db.flush()
    attempt = AgentRunAttempt(
        id=str(uuid4()), run_id=run.id, attempt=1, started_at=now, outcome="running"
    )
    db.add(attempt)
    await db.flush()
    await db.commit()
    return {"user": user, "session": session, "run": run, "attempt": attempt}


@pytest_asyncio.fixture
async def svc(db: AsyncSession, settings: Settings) -> PiEvidenceIngestService:
    events = AgentEventStream(db, AgentEventBroker())
    return PiEvidenceIngestService(db=db, events=events, settings=settings)


async def _event_types(db: AsyncSession, run_id: str) -> list[str]:
    rows = await db.scalars(
        select(AgentEvent.event_type).where(AgentEvent.run_id == run_id).order_by(AgentEvent.sequence)
    )
    return list(rows)


async def test_start_rejects_invalid_run_token(svc: PiEvidenceIngestService) -> None:
    with pytest.raises(HTTPException) as error:
        await svc.start_tool(
            token="not-a-token",
            run_id="run-any",
            request=PiToolStarted(call_id="pi-call-1", tool_name="kol_platform_search", arguments={}),
        )
    assert error.value.status_code == 401


async def test_start_unknown_run_404(
    svc: PiEvidenceIngestService, settings: Settings
) -> None:
    token = issue_run_token("missing-run", settings=settings)
    with pytest.raises(HTTPException) as error:
        await svc.start_tool(
            token=token,
            run_id="missing-run",
            request=PiToolStarted(call_id="pi-call-1", tool_name="kol_platform_search", arguments={}),
        )
    assert error.value.status_code == 404


async def test_start_creates_zero_point_step_and_tool_call(
    svc: PiEvidenceIngestService, settings: Settings, seeded: dict[str, Any]
) -> None:
    run_id = seeded["run"].id
    token = issue_run_token(run_id, settings=settings)

    response = await svc.start_tool(
        token=token,
        run_id=run_id,
        request=PiToolStarted(
            call_id="pi-call-1",
            tool_name="kol_platform_search",
            arguments={"keywords": "咖啡"},
        ),
    )

    assert response.call_id
    step = await svc._db.scalar(select(AgentStep).where(AgentStep.run_id == run_id))
    assert step is not None
    call = await svc._db.scalar(select(AgentToolCall).where(AgentToolCall.id == response.call_id))
    assert call is not None
    assert call.logical_call_id != "pi-call-1"
    assert call.points_reserved == 0
    assert call.points_settled == 0
    assert call.status in ("running", "planned")
    assert await _event_types(svc._db, run_id) == ["tool.started"]


async def test_start_idempotent_for_same_pi_call_id(
    svc: PiEvidenceIngestService, settings: Settings, seeded: dict[str, Any]
) -> None:
    run_id = seeded["run"].id
    token = issue_run_token(run_id, settings=settings)
    request = PiToolStarted(call_id="pi-call-1", tool_name="kol_platform_search", arguments={})

    first = await svc.start_tool(token=token, run_id=run_id, request=request)
    second = await svc.start_tool(token=token, run_id=run_id, request=request)

    assert first.call_id == second.call_id
    rows = await svc._db.scalars(select(AgentToolCall).where(AgentToolCall.run_id == run_id))
    assert len(list(rows)) == 1


async def test_settle_writes_evidence_and_zero_points(
    svc: PiEvidenceIngestService, settings: Settings, seeded: dict[str, Any]
) -> None:
    run_id = seeded["run"].id
    token = issue_run_token(run_id, settings=settings)
    started = await svc.start_tool(
        token=token,
        run_id=run_id,
        request=PiToolStarted(call_id="pi-call-1", tool_name="kol_platform_search", arguments={"q": "1"}),
    )
    raw_payload = {"result": '{"data": [1, 2], "total": 2}'}

    settled = await svc.settle_tool(
        token=token, run_id=run_id, call_id=started.call_id, request=PiToolSettled(raw_payload=raw_payload)
    )

    assert settled.evidence_id
    evidence = await svc._db.get(EvidenceItem, settled.evidence_id)
    assert evidence is not None
    assert evidence.availability_status == "available"
    assert evidence.tool_call_id == started.call_id
    assert evidence.payload_hash == hashlib.sha256(canonical_json_bytes(raw_payload)).hexdigest()
    call = await svc._db.get(AgentToolCall, started.call_id)
    assert call.status == "settled"
    assert call.points_reserved == 0
    assert call.points_settled == 0
    assert await _event_types(svc._db, run_id) == ["tool.started", "tool.succeeded"]


async def test_settle_idempotent_returns_same_evidence(
    svc: PiEvidenceIngestService, settings: Settings, seeded: dict[str, Any]
) -> None:
    run_id = seeded["run"].id
    token = issue_run_token(run_id, settings=settings)
    started = await svc.start_tool(
        token=token,
        run_id=run_id,
        request=PiToolStarted(call_id="pi-call-1", tool_name="kol_platform_search", arguments={}),
    )
    request = PiToolSettled(raw_payload={"data": [1]})

    first = await svc.settle_tool(token=token, run_id=run_id, call_id=started.call_id, request=request)
    second = await svc.settle_tool(token=token, run_id=run_id, call_id=started.call_id, request=request)

    assert first.evidence_id == second.evidence_id
    rows = await svc._db.scalars(select(EvidenceItem).where(EvidenceItem.tool_call_id == started.call_id))
    assert len(list(rows)) == 1


async def test_settle_cross_run_call_id_404(
    svc: PiEvidenceIngestService, settings: Settings, seeded: dict[str, Any]
) -> None:
    run_a = seeded["run"].id
    token_a = issue_run_token(run_a, settings=settings)
    started = await svc.start_tool(
        token=token_a,
        run_id=run_a,
        request=PiToolStarted(call_id="pi-call-1", tool_name="kol_platform_search", arguments={}),
    )

    other_run_id = str(uuid4())
    token_other = issue_run_token(other_run_id, settings=settings)
    with pytest.raises(HTTPException) as error:
        await svc.settle_tool(
            token=token_other,
            run_id=other_run_id,
            call_id=started.call_id,
            request=PiToolSettled(raw_payload={"data": [1]}),
        )
    assert error.value.status_code == 404


async def test_fail_and_unknown_produce_no_available_evidence(
    svc: PiEvidenceIngestService, settings: Settings, seeded: dict[str, Any]
) -> None:
    run_id = seeded["run"].id
    token = issue_run_token(run_id, settings=settings)

    failed = await svc.start_tool(
        token=token,
        run_id=run_id,
        request=PiToolStarted(call_id="pi-call-fail", tool_name="kol_platform_search", arguments={}),
    )
    await svc.fail_tool(
        token=token,
        run_id=run_id,
        call_id=failed.call_id,
        request=PiToolFailed(error={"error": "gateway_timeout"}, status="failed"),
    )

    unknown = await svc.start_tool(
        token=token,
        run_id=run_id,
        request=PiToolStarted(call_id="pi-call-unknown", tool_name="kol_platform_search", arguments={}),
    )
    await svc.fail_tool(
        token=token,
        run_id=run_id,
        call_id=unknown.call_id,
        request=PiToolFailed(error={"error": "possibly_sent"}, status="unknown"),
    )

    evidence_rows = await svc._db.scalars(
        select(EvidenceItem).where(EvidenceItem.availability_status == "available")
    )
    assert len(list(evidence_rows)) == 0
    assert await _event_types(svc._db, run_id) == [
        "tool.started",
        "tool.failed",
        "tool.started",
        "tool.unknown",
    ]
    failed_call = await svc._db.get(AgentToolCall, failed.call_id)
    unknown_call = await svc._db.get(AgentToolCall, unknown.call_id)
    assert failed_call.status == "failed"
    assert unknown_call.status == "unknown"
    assert failed_call.points_settled == 0
    assert unknown_call.points_settled == 0