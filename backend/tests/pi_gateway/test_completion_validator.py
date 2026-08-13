"""Pi Run 完成契约：assistant 不是唯一完成条件。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.agent_artifacts.models import (
    AgentArtifact,
    AgentArtifactVersion,
    ArtifactPublishAttempt,
    ArtifactDraft,
    ArtifactDraftRevision,
)
from app.agent_runtime.models import (
    AgentMessage,
    AgentRun,
    AgentRunAttempt,
    AgentStep,
    AgentToolCall,
    EvidenceItem,
)
from app.billing.models import TenantWalletTransaction
from app.pi_gateway.completion import CompletionValidator

from .test_model_usage import _run


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _assistant(db_session, run, text: str = "最终结论") -> None:
    db_session.add(
        AgentMessage(
            id=str(uuid4()),
            session_id=run.session_id,
            run_id=run.id,
            role="assistant",
            content=text,
            metadata_json={"gateway_message": True},
            sequence=1,
            created_at=_now(),
        )
    )
    await db_session.flush()


async def _publish_brand_report(db_session, run, *, source_run_id: str | None = None) -> None:
    now = _now()
    artifact_id = str(uuid4())
    draft_id = str(uuid4())
    revision_id = str(uuid4())
    if source_run_id is not None:
        db_session.add(
            AgentRun(
                id=source_run_id,
                session_id=run.session_id,
                user_id=run.user_id,
                tenant_id=run.tenant_id,
                runtime_backend="pi",
                runtime_config_version_id=None,
                runtime_config_snapshot_json={},
                profile_name="brand_analysis_v1",
                profile_version="v1",
                model="fake-model",
                status="completed",
                created_at=now,
            )
        )
        await db_session.flush()
    db_session.add(
        AgentArtifact(
            id=artifact_id,
            session_id=run.session_id,
            user_id=run.user_id,
            module="brand",
            artifact_type="brand_report_v3",
            artifact_key=f"brand-report-{artifact_id}",
            status="published",
            latest_version=1,
            activity_sequence=0,
            created_at=now,
            updated_at=now,
        )
    )
    db_session.add(
        ArtifactDraft(
            id=draft_id,
            artifact_id=artifact_id,
            session_id=run.session_id,
            owner_run_id=None,
            current_revision=1,
            status="idle",
            review_count=0,
            revision_count=1,
            updated_at=now,
        )
    )
    db_session.add(
        ArtifactDraftRevision(
            id=revision_id,
            draft_id=draft_id,
            artifact_id=artifact_id,
            run_id=source_run_id or run.id,
            revision=1,
            schema_version="brand_report_v3",
            payload_json={"data_status": "complete"},
            evidence_refs_json=[],
            payload_hash="a" * 64,
            created_at=now,
        )
    )
    await db_session.flush()
    lineage_snapshot = {"refs": []}
    if source_run_id is None:
        attempt = await db_session.scalar(
            select(AgentRunAttempt).where(AgentRunAttempt.run_id == run.id)
        )
        assert attempt is not None
        step_id = str(uuid4())
        tool_call_id = str(uuid4())
        evidence_id = str(uuid4())
        db_session.add(
            AgentStep(
                id=step_id,
                run_id=run.id,
                attempt_id=attempt.id,
                sequence=1,
                step_type="tool_call",
                status="completed",
                visibility="user",
                created_at=now,
            )
        )
        db_session.add(
            AgentToolCall(
                id=tool_call_id,
                run_id=run.id,
                step_id=step_id,
                logical_call_id=f"lineage-{tool_call_id}",
                service="insight-cube-mcp",
                internal_tool_name="query_analysis_data",
                arguments_hash="b" * 64,
                status="settled",
                points_reserved=10,
                points_settled=10,
                completed_at=now,
            )
        )
        db_session.add(
            EvidenceItem(
                id=evidence_id,
                session_id=run.session_id,
                run_id=run.id,
                tool_call_id=tool_call_id,
                source_type="mcp",
                source_name="query_analysis_data",
                raw_payload_json={"result": "[]"},
                normalized_preview_json={"result": "[]"},
                payload_hash="a" * 64,
                collected_at=now,
                availability_status="available",
            )
        )
        lineage_snapshot = {
            "refs": [
                {
                    "artifact_path": "/data_status",
                    "sources": [
                        {
                            "evidence_id": evidence_id,
                            "source_path": "",
                            "payload_hash": "a" * 64,
                            "tool_call_id": tool_call_id,
                            "tool_name": "query_analysis_data",
                            "service": "insight-cube-mcp",
                        }
                    ],
                }
            ]
        }
        await db_session.flush()
    version_id = str(uuid4())
    db_session.add(
        AgentArtifactVersion(
            id=version_id,
            artifact_id=artifact_id,
            version=1,
            source_run_id=source_run_id or run.id,
            source_draft_revision_id=revision_id,
            schema_version="brand_report_v3",
            payload_json={"data_status": "complete"},
            evidence_refs_json=[],
            lineage_snapshot_json=lineage_snapshot,
            review_json=None,
            validation_json={"valid": True},
            data_status="complete",
            created_at=now,
        )
    )
    db_session.add(
        ArtifactPublishAttempt(
            id=str(uuid4()),
            run_id=source_run_id or run.id,
            artifact_id=artifact_id,
            draft_revision_id=revision_id,
            status="published",
            idempotency_key=f"publish-{revision_id}",
            validation_json={"valid": True},
            published_version_id=version_id,
            created_at=now,
            completed_at=now,
        )
    )
    await db_session.flush()


@pytest.mark.asyncio
async def test_assistant_without_snapshot_required_artifact_is_rejected(db_session, user_factory):
    user = await user_factory()
    snapshot = {
        "required_artifact_contract": "brand_report_v3",
        "profile_name": "brand_analysis_v1",
        "capability_pack_version": "2026.08.12",
        "capability_pack_manifest_digest": "sha256:" + "a" * 64,
    }
    run, _attempt, _tenant_id = await _run(db_session, user, snapshot=snapshot)
    await _assistant(db_session, run)

    result = await CompletionValidator(db_session).validate(run)

    assert not result.ok
    assert result.code == "required_artifact_missing"


@pytest.mark.asyncio
async def test_tampered_capability_pack_audit_is_rejected(db_session, user_factory):
    user = await user_factory()
    run, _attempt, _tenant_id = await _run(
        db_session,
        user,
        snapshot={
            "required_artifact_contract": "brand_report_v3",
            "capability_pack_manifest_digest": "sha256:" + "b" * 64,
        },
    )
    await _assistant(db_session, run)

    result = await CompletionValidator(db_session).validate(run)

    assert not result.ok
    assert result.code == "required_artifact_missing"


@pytest.mark.asyncio
async def test_historical_artifact_cannot_satisfy_current_run(db_session, user_factory):
    user = await user_factory()
    snapshot = {"required_artifact_contract": "brand_report_v3"}
    run, _attempt, _tenant_id = await _run(db_session, user, snapshot=snapshot)
    await _assistant(db_session, run)
    await _publish_brand_report(db_session, run, source_run_id=str(uuid4()))

    result = await CompletionValidator(db_session).validate(run)

    assert not result.ok
    assert result.code == "required_artifact_missing"


@pytest.mark.asyncio
async def test_current_published_artifact_and_lineage_allow_completion(db_session, user_factory):
    user = await user_factory()
    snapshot = {"required_artifact_contract": "brand_report_v3"}
    run, _attempt, _tenant_id = await _run(db_session, user, snapshot=snapshot)
    await _assistant(db_session, run)
    await _publish_brand_report(db_session, run)

    result = await CompletionValidator(db_session).validate(run)

    assert result.ok
    assert result.code is None


@pytest.mark.asyncio
async def test_whitespace_assistant_is_not_a_durable_completion(db_session, user_factory):
    user = await user_factory()
    run, _attempt, _tenant_id = await _run(db_session, user)
    await _assistant(db_session, run, text=" \n\t ")

    result = await CompletionValidator(db_session).validate(run)

    assert not result.ok
    assert result.code == "pi_gateway_terminal_missing_completion"


@pytest.mark.asyncio
async def test_loop_guard_explanation_is_not_a_durable_completion(
    db_session, user_factory
) -> None:
    user = await user_factory()
    run, _attempt, _tenant_id = await _run(db_session, user)
    db_session.add(
        AgentMessage(
            id=str(uuid4()),
            session_id=run.session_id,
            run_id=run.id,
            role="assistant",
            content="系统已停止重复循环。",
            metadata_json={"system_loop_guard": True},
            sequence=1,
            created_at=_now(),
        )
    )
    await db_session.flush()

    result = await CompletionValidator(db_session).validate(run)

    assert not result.ok
    assert result.code == "pi_gateway_terminal_missing_completion"


@pytest.mark.asyncio
async def test_invalid_frozen_lineage_does_not_allow_completion(db_session, user_factory):
    user = await user_factory()
    run, _attempt, _tenant_id = await _run(
        db_session, user, snapshot={"required_artifact_contract": "brand_report_v3"}
    )
    await _assistant(db_session, run)
    await _publish_brand_report(db_session, run)
    version = await db_session.scalar(select(AgentArtifactVersion))
    assert version is not None
    version.lineage_snapshot_json = {"refs": [{"artifact_path": "/invalid", "sources": []}]}
    await db_session.flush()

    result = await CompletionValidator(db_session).validate(run)

    assert not result.ok
    assert result.code == "required_artifact_invalid_lineage"


@pytest.mark.asyncio
async def test_unresolved_tenant_permit_blocks_completion_even_without_tool_row(
    db_session, user_factory
):
    user = await user_factory()
    run, _attempt, tenant_id = await _run(db_session, user)
    await _assistant(db_session, run)
    db_session.add(
        TenantWalletTransaction(
            id=str(uuid4()),
            tenant_id=tenant_id,
            user_id=run.user_id,
            run_id=run.id,
            tool_call_id=str(uuid4()),
            internal_tool_name="query_analysis_data",
            kind="reserve",
            balance_delta=-10,
            reserved_delta=10,
            balance_after=990,
            reserved_after=10,
            idempotency_key=f"test-reserve-{uuid4()}",
            reference_type="mcp_call",
            reference_id=str(uuid4()),
            created_at=_now(),
        )
    )
    await db_session.flush()

    result = await CompletionValidator(db_session).validate(run)

    assert not result.ok
    assert result.code == "pi_gateway_unresolved_mcp_calls"


@pytest.mark.asyncio
async def test_unknown_tool_call_blocks_completion_even_with_assistant(db_session, user_factory):
    user = await user_factory()
    run, attempt, _tenant_id = await _run(db_session, user)
    await _assistant(db_session, run)
    now = _now()
    step = AgentStep(
        id=str(uuid4()),
        run_id=run.id,
        attempt_id=attempt.id,
        sequence=1,
        step_type="tool_call",
        status="completed",
        visibility="user",
        created_at=now,
    )
    db_session.add(step)
    await db_session.flush()
    db_session.add(
        AgentToolCall(
            id=str(uuid4()),
            run_id=run.id,
            step_id=step.id,
            logical_call_id=str(uuid4()),
            service="insight_cube",
            internal_tool_name="query_analysis_data",
            arguments_hash="b" * 64,
            status="unknown",
            points_reserved=10,
            started_at=now,
        )
    )
    await db_session.flush()

    result = await CompletionValidator(db_session).validate(run)

    assert not result.ok
    assert result.code == "pi_gateway_unresolved_mcp_calls"


@pytest.mark.asyncio
async def test_running_step_blocks_completion(db_session, user_factory):
    user = await user_factory()
    run, attempt, _tenant_id = await _run(db_session, user)
    await _assistant(db_session, run)
    db_session.add(
        AgentStep(
            id=str(uuid4()),
            run_id=run.id,
            attempt_id=attempt.id,
            sequence=1,
            step_type="model_turn",
            status="running",
            visibility="user",
            created_at=_now(),
        )
    )
    await db_session.flush()

    result = await CompletionValidator(db_session).validate(run)

    assert not result.ok
    assert result.code == "pi_gateway_running_agent_steps"
