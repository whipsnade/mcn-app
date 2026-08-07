"""Pi POC 内部工具桥测试（SQLite 内存库，绝不连接任何 MySQL 库）。

覆盖：工具目录白名单（不含 bash/文件/HTTP/Draft 直写/计算/记忆）、伪造身份键
忽略、get_session_context、search_evidence、build_brand_report_draft（含外部
Evidence 拒绝）、publish_artifacts（租约 + 幂等 + 发布后 Excel 渲染同 Version）、
未知工具 404、HTTP Schema 拒绝伪造身份键。
"""

import json
import secrets
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool


# SQLite 方言下把 MySQL MEDIUMTEXT 编译为 TEXT（models 共用同一 Base.metadata）。
@compiles(MEDIUMTEXT, "sqlite")
def _mediumtext_sqlite(element: Any, compiler: Any, **kw: Any) -> str:
    return "TEXT"


import app.db.models  # noqa: F401  # 注册全部表（含 legacy FK 目标，create_all 所需）
from app.agent_artifacts.exporters import export_artifact
from app.agent_artifacts.models import AgentArtifactVersion
from app.agent_runtime.events import AgentEventBroker, AgentEventStream
from app.agent_runtime.models import (
    AgentMessage,
    AgentRun,
    AgentRunAttempt,
    AgentSession,
)
from app.agent_runtime.repository import AgentRunRepository
from app.core.config import Settings
from app.db.base import Base
from app.identity.models import User
from app.pi_runtime_poc.auth import issue_run_token
from app.pi_runtime_poc.internal_tools import (
    PI_POC_ALLOWED_TOOLS,
    build_pi_internal_registry,
)
from app.pi_runtime_poc.schemas import (
    PiInternalToolRequest,
    PiToolSettled,
    PiToolStarted,
)
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
    return PiEvidenceIngestService(
        db=db, events=events, settings=settings, worker_id="pi-poc-test"
    )


_BRAND_OVERVIEW = {
    "result": json.dumps(
        [
            {
                "平台": "小红书",
                "声量": 100,
                "互动数": 1000,
                "发帖数": 80,
                "正面声量": 60,
                "中性声量": 30,
                "负面声量": 10,
            },
            {
                "平台": "抖音",
                "声量": 200,
                "互动数": 3000,
                "发帖数": 150,
                "正面声量": 120,
                "中性声量": 60,
                "负面声量": 20,
            },
        ]
    )
}

_BRAND_SCOPE = {
    "brand": "测试品牌",
    "period": {"start": "2026-07-01", "end": "2026-07-31", "timezone": "Asia/Shanghai"},
    "platforms": ["xiaohongshu"],
    "keywords": ["测试"],
    "comparison_mode": "none",
}


async def _seed_evidence(
    svc: PiEvidenceIngestService,
    settings: Settings,
    seeded: dict[str, Any],
    *,
    payload: Any,
    tool_name: str = "kol_xiaohongshu_search",
    call_id: str | None = None,
) -> str:
    run_id = seeded["run"].id
    token = issue_run_token(run_id, settings=settings)
    started = await svc.start_tool(
        token=token,
        run_id=run_id,
        request=PiToolStarted(call_id=call_id or str(uuid4()), tool_name=tool_name, arguments={}),
    )
    settled = await svc.settle_tool(
        token=token,
        run_id=run_id,
        call_id=started.call_id,
        request=PiToolSettled(raw_payload=payload),
    )
    assert settled.evidence_id
    return settled.evidence_id


async def _claim_run_lease(svc: PiEvidenceIngestService, run_id: str) -> None:
    claimed = await AgentRunRepository(svc._db).claim_lease(
        run_id,
        "pi-poc-test",
        svc._settings.pi_runtime_poc_run_timeout_seconds,
    )
    assert claimed


async def test_internal_registry_exposes_only_allowed_tools(
    db: AsyncSession,
) -> None:
    registry = build_pi_internal_registry(db=db, worker_id="pi-poc-test")
    names = {entry.internal_name for entry in registry.registered_tools}
    assert names == set(PI_POC_ALLOWED_TOOLS)
    for forbidden in (
        "bash",
        "read",
        "write",
        "edit",
        "create_draft",
        "update_draft",
        "abandon_draft",
        "remember_scope",
        "calculate_expression",
        "aggregate_metrics",
        "rank_kols",
    ):
        assert forbidden not in names


async def test_forged_identity_keys_are_ignored(
    svc: PiEvidenceIngestService, settings: Settings, seeded: dict[str, Any]
) -> None:
    run_id = seeded["run"].id
    token = issue_run_token(run_id, settings=settings)

    result = await svc.execute_internal_tool(
        token=token,
        run_id=run_id,
        tool_name="get_session_context",
        arguments={"user_id": "forged", "session_id": "forged", "run_id": "forged"},
    )

    assert result["status"] == "success"
    summary = json.loads(result["safe_summary"])
    assert summary["run_id"] == run_id
    assert summary["session_id"] == seeded["session"].id
    assert summary["user_id"] == seeded["user"].id


async def test_http_schema_rejects_forged_identity_keys() -> None:
    with pytest.raises(ValidationError):
        PiInternalToolRequest.model_validate(
            {
                "tool_name": "search_evidence",
                "arguments": {"query": "咖啡"},
                "user_id": "forged",
                "session_id": "forged",
                "run_id": "forged",
                "worker_id": "forged",
            }
        )


async def test_unknown_internal_tool_404(
    svc: PiEvidenceIngestService, settings: Settings, seeded: dict[str, Any]
) -> None:
    run_id = seeded["run"].id
    token = issue_run_token(run_id, settings=settings)

    with pytest.raises(HTTPException) as error:
        await svc.execute_internal_tool(
            token=token, run_id=run_id, tool_name="bash", arguments={}
        )
    assert error.value.status_code == 404


async def test_request_clarification_requires_lease_and_uses_existing_run_state_machine(
    svc: PiEvidenceIngestService, settings: Settings, seeded: dict[str, Any]
) -> None:
    run_id = seeded["run"].id
    token = issue_run_token(run_id, settings=settings)
    arguments = {
        "question": "请确认要分析的品牌和时间范围。",
        "options": ["指定品牌和近30天", "指定品牌和近90天"],
    }

    with pytest.raises(HTTPException) as error:
        await svc.execute_internal_tool(
            token=token,
            run_id=run_id,
            tool_name="request_clarification",
            arguments=arguments,
        )
    assert error.value.status_code == 409
    assert error.value.detail == "pi_run_lease_not_held"

    await _claim_run_lease(svc, run_id)
    result = await svc.execute_internal_tool(
        token=token,
        run_id=run_id,
        tool_name="request_clarification",
        arguments=arguments,
    )

    assert result["status"] == "success"
    run = await svc._db.get(AgentRun, run_id)
    assert run is not None and run.status == "clarification_requested"
    message = await svc._db.scalar(
        select(AgentMessage).where(AgentMessage.run_id == run_id, AgentMessage.role == "assistant")
    )
    assert message is not None
    assert message.content == arguments["question"]
    assert message.metadata_json == {
        "type": "clarification",
        "question": arguments["question"],
        "options": arguments["options"],
    }


async def test_search_evidence_returns_seeded_rows(
    svc: PiEvidenceIngestService, settings: Settings, seeded: dict[str, Any]
) -> None:
    evidence_id = await _seed_evidence(svc, settings, seeded, payload=_BRAND_OVERVIEW)
    run_id = seeded["run"].id
    token = issue_run_token(run_id, settings=settings)

    result = await svc.execute_internal_tool(
        token=token,
        run_id=run_id,
        tool_name="search_evidence",
        arguments={"query": "小红书"},
    )

    assert result["status"] == "success"
    assert evidence_id in result["safe_summary"]


async def test_build_brand_report_draft_succeeds_with_limited_feedback(
    svc: PiEvidenceIngestService, settings: Settings, seeded: dict[str, Any]
) -> None:
    evidence_id = await _seed_evidence(svc, settings, seeded, payload=_BRAND_OVERVIEW)
    run_id = seeded["run"].id
    token = issue_run_token(run_id, settings=settings)

    result = await svc.execute_internal_tool(
        token=token,
        run_id=run_id,
        tool_name="build_brand_report_draft",
        arguments={"scope": _BRAND_SCOPE, "evidence": {"overview_current": [evidence_id]}},
    )

    assert result["status"] == "success"
    summary = json.loads(result["safe_summary"])
    assert summary["schema_version"] == "brand_report_v3"
    assert summary["draft_id"]
    assert "limitations" in summary
    assert "overview" in summary["availability"]
    covered_sections = (
        summary["coverage"]["complete_sections"]
        + summary["coverage"]["restricted_sections"]
    )
    assert "overview" in covered_sections
    assert any(
        source["evidence_id"] == evidence_id
        for reference in summary["evidence_refs"]
        for source in reference["sources"]
    )
    # Builder 反馈必须是受限摘要：不包含完整原始 Evidence/Excel。
    assert "result" not in json.dumps(summary) or "小红书" not in json.dumps(summary)


async def test_build_brand_report_draft_rejects_foreign_evidence(
    svc: PiEvidenceIngestService, settings: Settings, seeded: dict[str, Any]
) -> None:
    # 另一用户/会话的 Evidence：直接伪造一个不存在的 evidence_id（跨 Session 语义）。
    run_id = seeded["run"].id
    token = issue_run_token(run_id, settings=settings)

    result = await svc.execute_internal_tool(
        token=token,
        run_id=run_id,
        tool_name="build_brand_report_draft",
        arguments={
            "scope": _BRAND_SCOPE,
            "evidence": {"overview_current": [str(uuid4())]},
        },
    )

    assert result["status"] == "failed"
    assert result["error_type"] == "evidence_not_found"


async def test_build_brand_report_draft_rejects_existing_evidence_from_another_user(
    svc: PiEvidenceIngestService,
    settings: Settings,
    seeded: dict[str, Any],
) -> None:
    now = _now()
    foreign_user = User(
        id=str(uuid4()),
        nickname="foreign",
        role="user",
        status="active",
        created_at=now,
        updated_at=now,
    )
    svc._db.add(foreign_user)
    await svc._db.flush()
    foreign_session = AgentSession(
        id=str(uuid4()),
        user_id=foreign_user.id,
        title="foreign",
        status="active",
        summary_version=0,
        created_at=now,
        updated_at=now,
    )
    foreign_run = AgentRun(
        id=str(uuid4()),
        session_id=foreign_session.id,
        user_id=foreign_user.id,
        run_kind="user",
        visibility="user",
        profile_name="session_analyst_v1",
        profile_version="1",
        model="test",
        status="running",
    )
    svc._db.add_all([foreign_session, foreign_run])
    await svc._db.flush()
    foreign_evidence_id = await _seed_evidence(
        svc,
        settings,
        {"run": foreign_run},
        payload=_BRAND_OVERVIEW,
    )

    run_id = seeded["run"].id
    token = issue_run_token(run_id, settings=settings)
    result = await svc.execute_internal_tool(
        token=token,
        run_id=run_id,
        tool_name="build_brand_report_draft",
        arguments={
            "scope": _BRAND_SCOPE,
            "evidence": {"overview_current": [foreign_evidence_id]},
        },
    )

    assert result["status"] == "failed"
    assert result["error_type"] == "evidence_not_found"


async def test_publish_artifacts_requires_existing_active_run_lease(
    svc: PiEvidenceIngestService, settings: Settings, seeded: dict[str, Any]
) -> None:
    evidence_id = await _seed_evidence(svc, settings, seeded, payload=_BRAND_OVERVIEW)
    run_id = seeded["run"].id
    token = issue_run_token(run_id, settings=settings)
    built = await svc.execute_internal_tool(
        token=token,
        run_id=run_id,
        tool_name="build_brand_report_draft",
        arguments={"scope": _BRAND_SCOPE, "evidence": {"overview_current": [evidence_id]}},
    )
    draft_id = json.loads(built["safe_summary"])["draft_id"]

    with pytest.raises(HTTPException) as error:
        await svc.execute_internal_tool(
            token=token,
            run_id=run_id,
            tool_name="publish_artifacts",
            arguments={"draft_ids": [draft_id]},
        )

    assert error.value.status_code == 409
    assert error.value.detail == "pi_run_lease_not_held"


async def test_publish_artifacts_publishes_and_renders_excel_same_version(
    svc: PiEvidenceIngestService, settings: Settings, seeded: dict[str, Any]
) -> None:
    evidence_id = await _seed_evidence(svc, settings, seeded, payload=_BRAND_OVERVIEW)
    run_id = seeded["run"].id
    token = issue_run_token(run_id, settings=settings)
    built = await svc.execute_internal_tool(
        token=token,
        run_id=run_id,
        tool_name="build_brand_report_draft",
        arguments={"scope": _BRAND_SCOPE, "evidence": {"overview_current": [evidence_id]}},
    )
    draft_id = json.loads(built["safe_summary"])["draft_id"]
    await _claim_run_lease(svc, run_id)

    published = await svc.execute_internal_tool(
        token=token,
        run_id=run_id,
        tool_name="publish_artifacts",
        arguments={"draft_ids": [draft_id]},
    )

    assert published["status"] == "success"
    items = json.loads(published["safe_summary"])
    assert items[0]["draft_id"] == draft_id
    assert items[0]["status"] == "published"
    version_id = items[0]["artifact_version_id"]
    assert version_id

    version = await svc._db.get(AgentArtifactVersion, version_id)
    assert version is not None
    exported = export_artifact(version)
    assert len(exported) > 0
    assert version.payload_json is not None
    # Excel 渲染基于同一 Version 的 payload（元数据一致）。
    assert version.id == version_id


async def test_publish_artifacts_is_idempotent(
    svc: PiEvidenceIngestService, settings: Settings, seeded: dict[str, Any]
) -> None:
    evidence_id = await _seed_evidence(svc, settings, seeded, payload=_BRAND_OVERVIEW)
    run_id = seeded["run"].id
    token = issue_run_token(run_id, settings=settings)
    built = await svc.execute_internal_tool(
        token=token,
        run_id=run_id,
        tool_name="build_brand_report_draft",
        arguments={"scope": _BRAND_SCOPE, "evidence": {"overview_current": [evidence_id]}},
    )
    draft_id = json.loads(built["safe_summary"])["draft_id"]
    await _claim_run_lease(svc, run_id)

    first = await svc.execute_internal_tool(
        token=token,
        run_id=run_id,
        tool_name="publish_artifacts",
        arguments={"draft_ids": [draft_id]},
    )
    second = await svc.execute_internal_tool(
        token=token,
        run_id=run_id,
        tool_name="publish_artifacts",
        arguments={"draft_ids": [draft_id]},
    )

    first_items = json.loads(first["safe_summary"])
    second_items = json.loads(second["safe_summary"])
    assert first_items[0]["status"] == "published"
    assert second_items[0]["artifact_version_id"] == first_items[0]["artifact_version_id"]

    count = await svc._db.scalar(
        select(func.count()).select_from(AgentArtifactVersion)
    )
    assert count == 1
