"""Pi 路径 Artifact 生命周期 SSE 事件的补发契约（对齐 agent engine 形状）。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.agent_artifacts.models import AgentArtifact
from app.agent_runtime.models import AgentEvent, AgentRun, AgentRunAttempt, AgentSession
from app.agent_runtime.tools.contracts import ToolResult
from app.pi_gateway.internal_tools import append_artifact_tool_events



def _payload(event) -> dict:
    raw = event.payload_json
    return json.loads(raw) if isinstance(raw, str) else raw

def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _seed_run(db_session, user_factory) -> AgentRun:
    user = await user_factory()
    from app.tenancy.models import TenantMembership
    membership = await db_session.scalar(
        select(TenantMembership).where(TenantMembership.user_id == user.id)
    )
    assert membership is not None
    now = _now()
    session = AgentSession(
        id=str(uuid4()), user_id=user.id, tenant_id=membership.tenant_id, title="pi artifact events",
        status="active", created_at=now, updated_at=now,
    )
    db_session.add(session)
    await db_session.flush()
    run = AgentRun(
        id=str(uuid4()), session_id=session.id, user_id=user.id, tenant_id=membership.tenant_id,
        runtime_backend="pi", runtime_config_version_id=None, runtime_config_snapshot_json=None,
        queued_at=None, profile_name="pi_production", profile_version="v1", model="fake-model",
        status="running", decision_count=0, review_count=0, revision_count=0,
        created_at=now, started_at=now, run_kind="user",
    )
    db_session.add(run)
    db_session.add(AgentRunAttempt(id=str(uuid4()), run_id=run.id, attempt=1, started_at=now, outcome="running"))
    await db_session.flush()
    return run


async def _seed_artifact(db_session, run: AgentRun) -> AgentArtifact:
    artifact = AgentArtifact(
        id=str(uuid4()), session_id=run.session_id, user_id=run.user_id, module="brand",
        artifact_type="brand_report_v3", parent_artifact_id=None,
        artifact_key=f"brand:test-{uuid4().hex[:8]}", status="published", latest_version=1,
        created_at=_now(), updated_at=_now(),
    )
    db_session.add(artifact)
    await db_session.flush()
    return artifact


@pytest.mark.asyncio
async def test_build_draft_success_emits_artifact_draft_created(db_session, user_factory) -> None:
    run = await _seed_run(db_session, user_factory)
    artifact = await _seed_artifact(db_session, run)
    result = ToolResult(
        status="success",
        safe_summary=json.dumps({
            "artifact_id": artifact.id, "draft_id": "draft-1", "revision": 1,
        }),
    )

    events = await append_artifact_tool_events(db_session, run, "build_artifact_draft", result)

    assert len(events) == 1
    stored = (await db_session.scalars(
        select(AgentEvent).where(AgentEvent.run_id == run.id)
    )).all()
    assert len(stored) == 1
    assert stored[0].event_type == "artifact.draft.created"
    payload = _payload(stored[0])
    assert payload["artifact_id"] == artifact.id
    assert payload["draft_id"] == "draft-1"
    assert payload["module"] == "brand"
    assert payload["parent_artifact_id"] is None
    assert payload["status"] == "published"
    assert payload["version"] == 1
    assert payload["run_id"] == run.id


@pytest.mark.asyncio
async def test_draft_revision_over_one_is_reported_as_updated(db_session, user_factory) -> None:
    run = await _seed_run(db_session, user_factory)
    artifact = await _seed_artifact(db_session, run)
    result = ToolResult(
        status="success",
        safe_summary=json.dumps({
            "artifact_id": artifact.id, "draft_id": "draft-2", "revision": 3,
        }),
    )

    events = await append_artifact_tool_events(db_session, run, "build_artifact_draft", result)

    assert [event.event_type for event in events] == ["artifact.draft.updated"]
    payload = _payload(events[0])
    assert payload["version"] == 3


@pytest.mark.asyncio
async def test_publish_success_emits_one_event_per_published_artifact(db_session, user_factory) -> None:
    run = await _seed_run(db_session, user_factory)
    artifact_a = await _seed_artifact(db_session, run)
    artifact_b = await _seed_artifact(db_session, run)
    result = ToolResult(
        status="success",
        safe_summary=json.dumps([
            {"draft_id": "draft-a", "status": "published", "artifact_id": artifact_a.id,
             "artifact_version_id": "v-a", "version": 2, "errors": []},
            {"draft_id": "draft-b", "status": "validation_failed", "artifact_id": artifact_b.id,
             "artifact_version_id": None, "version": None, "errors": [{"code": "x"}]},
        ]),
    )

    events = await append_artifact_tool_events(db_session, run, "publish_artifacts", result)

    assert [event.event_type for event in events] == ["artifact.published"]
    payload = _payload(events[0])
    assert payload["artifact_id"] == artifact_a.id
    assert payload["module"] == "brand"
    assert payload["version"] == 2


@pytest.mark.asyncio
async def test_failed_tool_result_emits_nothing(db_session, user_factory) -> None:
    run = await _seed_run(db_session, user_factory)
    result = ToolResult(status="failed", safe_summary="artifact_payload_invalid", error_type="artifact_payload_invalid")

    events = await append_artifact_tool_events(db_session, run, "build_artifact_draft", result)

    assert events == []
    stored = (await db_session.scalars(
        select(AgentEvent).where(AgentEvent.run_id == run.id)
    )).all()
    assert stored == []


@pytest.mark.asyncio
async def test_sequence_appends_after_existing_max(db_session, user_factory) -> None:
    from app.agent_runtime.events import AgentEventStream
    from app.agent_runtime.events import AgentEventBroker

    run = await _seed_run(db_session, user_factory)
    artifact = await _seed_artifact(db_session, run)
    stream = AgentEventStream(db_session, AgentEventBroker())
    await stream.append_locked(run, "tool.started", {"x": 1})

    result = ToolResult(
        status="success",
        safe_summary=json.dumps({"artifact_id": artifact.id, "draft_id": "d", "revision": 1}),
    )
    await append_artifact_tool_events(db_session, run, "build_artifact_draft", result)

    stored = (await db_session.scalars(
        select(AgentEvent).where(AgentEvent.run_id == run.id).order_by(AgentEvent.sequence)
    )).all()
    assert [event.sequence for event in stored] == [1, 2]
    assert stored[-1].event_type == "artifact.draft.created"
