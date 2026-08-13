from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.agent_artifacts.models import AgentArtifactVersion, ArtifactDraftRevision
from app.agent_artifacts.publishing import ArtifactPublicationService
from app.agent_runtime.models import AgentMessage, AgentRun, AgentSession, EvidenceItem
from app.agent_runtime.repository import AgentRunRepository
from app.agent_runtime.tools.builders import BuildArtifactDraftArgs, BuildArtifactDraftTool
from app.agent_runtime.tools.contracts import ToolContext
from app.pi_gateway.completion import CompletionValidator

from tests.agent_artifacts.payload_fixtures import brand_payload, insight_payload


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.mark.asyncio
async def test_direct_builder_accepts_model_payload_without_evidence(db_session, user_factory) -> None:
    user = await user_factory()
    now = _now()
    session = AgentSession(
        id=str(uuid4()), user_id=user.id, title="direct artifact", status="active",
        created_at=now, updated_at=now,
    )
    db_session.add(session)
    await db_session.flush()
    run = AgentRun(
        id=str(uuid4()), session_id=session.id, user_id=user.id, profile_name="session_analyst_v1",
        profile_version="v1", model="test-model", runtime_backend="pi", status="running",
        runtime_config_snapshot_json={
                "allowed_artifact_contracts": ["brand_report_v3"],
            "capability_pack_version": "1.0.0",
            "capability_pack_manifest_digest": "sha256:" + "a" * 64,
        },
        created_at=now, started_at=now, run_kind="user",
    )
    db_session.add(run)
    await db_session.flush()

    result = await BuildArtifactDraftTool(db_session).execute(
        ToolContext(
            user_id=user.id, session_id=session.id, run_id=run.id,
            profile_name="session_analyst_v1",
        ),
            BuildArtifactDraftArgs(artifact_type="brand_report_v3", payload=brand_payload()),
    )

    assert result.status == "success"
    assert await db_session.scalar(select(EvidenceItem.id).where(EvidenceItem.run_id == run.id)) is None
    revision = await db_session.scalar(select(ArtifactDraftRevision).where(ArtifactDraftRevision.run_id == run.id))
    assert revision is not None
    assert revision.schema_version == "brand_report_v3"


@pytest.mark.asyncio
async def test_direct_builder_publishes_without_fabricated_evidence(db_session, user_factory) -> None:
    user = await user_factory()
    now = _now()
    session = AgentSession(
        id=str(uuid4()), user_id=user.id, title="direct publication", status="active",
        created_at=now, updated_at=now,
    )
    db_session.add(session)
    await db_session.flush()
    run = AgentRun(
        id=str(uuid4()), session_id=session.id, user_id=user.id, profile_name="session_analyst_v1",
        profile_version="v1", model="test-model", runtime_backend="pi", status="running",
        runtime_config_snapshot_json={
            "allowed_artifact_contracts": ["brand_report_v3"],
            "capability_pack": {"pack_version": "1.0.0", "manifest_digest": "sha256:" + "a" * 64},
            "capability_pack_version": "1.0.0",
            "capability_pack_manifest_digest": "sha256:" + "a" * 64,
        },
        created_at=now, started_at=now, run_kind="user",
    )
    db_session.add(run)
    await db_session.flush()
    assert await AgentRunRepository(db_session).claim_lease(run.id, "direct-worker", 300)

    result = await BuildArtifactDraftTool(db_session).execute(
        ToolContext(user_id=user.id, session_id=session.id, run_id=run.id, profile_name="session_analyst_v1"),
        BuildArtifactDraftArgs(artifact_type="brand_report_v3", payload=brand_payload()),
    )
    assert result.status == "success"
    draft_id = (await db_session.scalar(select(ArtifactDraftRevision).where(ArtifactDraftRevision.run_id == run.id))).draft_id
    published = await ArtifactPublicationService(db_session).publish(
        run_id=run.id, draft_ids=(draft_id,), worker_id="direct-worker"
    )
    assert published[0].status == "published"
    version = await db_session.scalar(
        select(AgentArtifactVersion).where(AgentArtifactVersion.source_run_id == run.id)
    )
    assert version is not None
    assert version.evidence_refs_json == []
    assert version.lineage_snapshot_json == {"mode": "model_direct_v1", "refs": [], "source_tool_call_ids": []}
    assert await db_session.scalar(select(EvidenceItem.id).where(EvidenceItem.run_id == run.id)) is None
    db_session.add(
        AgentMessage(
            id=str(uuid4()), session_id=session.id, run_id=run.id, role="assistant",
            content="报告已生成", metadata_json={}, sequence=1, created_at=now,
        )
    )
    await db_session.flush()
    completion = await CompletionValidator(db_session).validate(run)
    assert completion.ok
    assert completion.artifact_version_id == version.id


@pytest.mark.asyncio
async def test_direct_builder_rejects_contract_not_in_snapshot(db_session, user_factory) -> None:
    user = await user_factory()
    now = _now()
    session = AgentSession(
        id=str(uuid4()), user_id=user.id, title="direct artifact", status="active",
        created_at=now, updated_at=now,
    )
    db_session.add(session)
    await db_session.flush()
    run = AgentRun(
        id=str(uuid4()), session_id=session.id, user_id=user.id, profile_name="session_analyst_v1",
        profile_version="v1", model="test-model", runtime_backend="pi", status="running",
        runtime_config_snapshot_json={"allowed_artifact_contracts": ["brand_report_v3"]},
        created_at=now, started_at=now, run_kind="user",
    )
    db_session.add(run)
    await db_session.flush()

    result = await BuildArtifactDraftTool(db_session).execute(
        ToolContext(user_id=user.id, session_id=session.id, run_id=run.id, profile_name="session_analyst_v1"),
        BuildArtifactDraftArgs(artifact_type="insight_board_v1", payload=insight_payload()),
    )

    assert result.status == "failed"
    assert result.error_type == "artifact_contract_not_allowed"
