"""Task 4 structured publication lineage tests (先红灯，再接入门禁)."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import func, select

import app.agent_artifacts.publishing as publishing_module
from app.agent_artifacts.canonical import walk_data_leaves
from app.agent_artifacts.lineage import (
    EvidenceRecord,
    LineageError,
    LineageOwner,
    validate_and_freeze_lineage,
    validate_structured_claims,
)
from app.agent_artifacts.models import (
    AgentArtifactVersion,
    ArtifactPublishAttempt,
    ArtifactReviewBatch,
    ArtifactReviewItem,
)
from app.agent_artifacts.publishing import ArtifactPublicationService
from app.agent_artifacts.service import ArtifactService, PublishBlocked
from app.agent_runtime.models import AgentRunAttempt, AgentStep, AgentToolCall, EvidenceItem
from app.agent_runtime.repository import AgentRunRepository, utc_now
from app.agent_runtime.state import RunStatus
from tests.agent_artifacts.test_payloads import build_brand_dict

WORKER = "publication-lineage-worker"


def _structured_payload(
    *,
    value: Any = 10,
    availability: str = "complete",
    evidence_ids: list[str] | None = None,
    supporting_paths: list[str] | None = None,
    field_lineage: list[str] | None = None,
    limitation_paths: list[str] | None = None,
    artifact_version_id: str | None = None,
) -> dict[str, Any]:
    field: dict[str, Any] = {
        "path": "/data/overview/total_volume",
        "value": value,
        "availability": availability,
        "evidence_ids": evidence_ids if evidence_ids is not None else ["ev-ok"],
        "unit": "mentions",
    }
    if artifact_version_id is not None:
        # 纯 validator 契约保留候选 Version 归属检查；强类型 payload 会在
        # 进入发布边界前拒绝未知字段，避免把版本身份写进公开 Artifact。
        field["artifact_version_id"] = artifact_version_id
    finding: dict[str, Any] = {
        "title": "总量",
        "detail": "结构化结论",
    }
    if supporting_paths is not None:
        finding["supporting_paths"] = supporting_paths
    payload: dict[str, Any] = {
        "data": {"overview": {"total_volume": value}},
        "canonical_data": [field],
        "field_lineage": {
            "/data/overview/total_volume": field_lineage
            if field_lineage is not None
            else ["/data/overview/total_volume"]
        },
        "availability": {
            "overview": {"status": availability, "reason_codes": []}
        },
        "limitations": [
            {
                "code": "limited",
                "message": "部分数据",
                "affected_paths": limitation_paths or [],
            }
        ]
        if limitation_paths is not None
        else [],
        "narrative": {
            "findings": [finding]
        },
    }
    return payload


def _scope(*, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "user_id": "user-1",
        "session_id": "session-1",
        "run_id": "run-1",
        "evidence": evidence
        if evidence is not None
        else {
            "ev-ok": {
                "user_id": "user-1",
                "session_id": "session-1",
                "run_id": "run-1",
                "source_type": "mcp",
            }
        },
    }


def _codes(issues: list[Any]) -> set[str]:
    return {issue.code for issue in issues}


def test_structured_claims_rejects_missing_or_empty_supporting_path() -> None:
    for paths in (None, []):
        issues = validate_structured_claims(
            _structured_payload(supporting_paths=paths), "version-1", _scope()
        )
        assert "supporting_path_missing" in _codes(issues)


def test_structured_claims_rejects_nonexistent_supporting_path() -> None:
    issues = validate_structured_claims(
        _structured_payload(supporting_paths=["overview.nope"]), "version-1", _scope()
    )
    assert "supporting_path_not_found" in _codes(issues)


def test_structured_claims_rejects_non_candidate_version_lineage() -> None:
    issues = validate_structured_claims(
        _structured_payload(artifact_version_id="version-other"),
        "version-1",
        _scope(),
    )
    assert "canonical_version_mismatch" in _codes(issues)


def test_structured_claims_rejects_unavailable_narrative_field() -> None:
    issues = validate_structured_claims(
        _structured_payload(
            value=None,
            availability="unavailable",
            evidence_ids=[],
            supporting_paths=["overview.total_volume"],
        ),
        "version-1",
        _scope(),
    )
    assert "supporting_path_unavailable" in _codes(issues)


def test_structured_claims_rejects_canonical_value_mismatch() -> None:
    payload = _structured_payload()
    payload["canonical_data"][0]["value"] = 11
    issues = validate_structured_claims(payload, "version-1", _scope())
    assert "canonical_value_mismatch" in _codes(issues)


def test_structured_claims_rejects_field_lineage_mismatch() -> None:
    issues = validate_structured_claims(
        _structured_payload(field_lineage=["/data/overview/other"]),
        "version-1",
        _scope(),
    )
    assert "field_lineage_mismatch" in _codes(issues)


def test_structured_claims_rejects_partial_without_covering_limitation() -> None:
    issues = validate_structured_claims(
        _structured_payload(
            availability="partial", limitation_paths=["/data/sentiment/score"]
        ),
        "version-1",
        _scope(),
    )
    assert "partial_without_limitation" in _codes(issues)


def test_structured_claims_rejects_evidence_from_other_session() -> None:
    issues = validate_structured_claims(
        _structured_payload(supporting_paths=["overview.total_volume"]),
        "version-1",
        _scope(
            evidence={
                "ev-ok": {
                    "user_id": "user-1",
                    "session_id": "session-other",
                    "run_id": "run-1",
                    "source_type": "mcp",
                }
            }
        ),
    )
    assert "evidence_session_mismatch" in _codes(issues)


def test_structured_claims_rejects_mcp_evidence_from_other_run() -> None:
    issues = validate_structured_claims(
        _structured_payload(),
        "version-1",
        _scope(
            evidence={
                "ev-ok": {
                    "user_id": "user-1",
                    "session_id": "session-1",
                    "run_id": "run-other",
                    "source_type": "mcp",
                }
            }
        ),
    )
    assert "evidence_run_mismatch" in _codes(issues)


def test_structured_claims_rejects_evidence_ids_not_matching_field_lineage() -> None:
    scope = _scope()
    scope["field_evidence_ids"] = {"/data/overview/total_volume": {"ev-other"}}
    issues = validate_structured_claims(
        _structured_payload(supporting_paths=["overview.total_volume"]),
        "version-1",
        scope,
    )
    assert "evidence_lineage_mismatch" in _codes(issues)


def test_structured_claims_allows_pre_run_uploaded_evidence() -> None:
    issues = validate_structured_claims(
        _structured_payload(supporting_paths=["overview.total_volume"]),
        "version-1",
        _scope(
            evidence={
                "ev-ok": {
                    "user_id": "user-1",
                    "session_id": "session-1",
                    "run_id": None,
                    "source_type": "user_upload",
                }
            }
        ),
    )
    assert issues == []


def test_structured_claims_rejects_uploaded_evidence_bound_to_other_run() -> None:
    issues = validate_structured_claims(
        _structured_payload(supporting_paths=["overview.total_volume"]),
        "version-1",
        _scope(
            evidence={
                "ev-ok": {
                    "user_id": "user-1",
                    "session_id": "session-1",
                    "run_id": "run-other",
                    "source_type": "user_upload",
                }
            }
        ),
    )
    assert "evidence_run_mismatch" in _codes(issues)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("metadata_key", "metadata_value"),
    (
        ("_user_id", "user-other"),
        ("_session_id", "session-other"),
        ("_run_id", "run-other"),
    ),
)
async def test_lineage_rejects_upload_metadata_outside_owner(
    metadata_key: str, metadata_value: str
) -> None:
    class _Loader:
        def __init__(self) -> None:
            self.record = EvidenceRecord(
                id="ev-upload",
                session_id="session-1",
                raw_payload={"value": 1},
                payload_hash="hash",
                run_id=None,
                upload={"upload_id": "upload-1", metadata_key: metadata_value},
            )

        async def load_evidence(self, _evidence_id: str) -> EvidenceRecord:
            return self.record

        async def load_artifact_version(self, _version_id: str):
            return None

        async def load_tool_call(self, _tool_call_id: str):
            return None

    with pytest.raises(LineageError) as exc_info:
        await validate_and_freeze_lineage(
            payload={"data": {"value": 1}},
            refs=[
                {
                    "artifact_path": "/data/value",
                    "sources": [
                        {
                            "source_type": "evidence",
                            "evidence_id": "ev-upload",
                            "source_path": "/value",
                        }
                    ],
                }
            ],
            owner=LineageOwner(
                user_id="user-1", session_id="session-1", run_id="run-1"
            ),
            loader=_Loader(),
        )

    assert exc_info.value.code in {
        "evidence_upload_not_owned",
        "evidence_run_not_owned",
    }


def test_structured_claims_accepts_legal_brand_and_campaign_payloads() -> None:
    from tests.agent_artifacts.test_payloads import build_campaign_dict

    for payload in (build_brand_dict(), build_campaign_dict()):
        issues = validate_structured_claims(
            payload,
            "version-1",
            {
                "user_id": "user-1",
                "session_id": "session-1",
                "run_id": "run-1",
                "evidence": {
                    "fixture-evidence": {
                        "user_id": "user-1",
                        "session_id": "session-1",
                        "run_id": "run-1",
                        "source_type": "mcp",
                    }
                },
            },
        )
        assert issues == []


async def _write_evidence(
    db_session, run, payload: dict[str, Any], *, evidence_id: str = "ev-publication"
) -> str:
    """Create a settled internal call and one Evidence covering the fixture data."""
    now = utc_now()
    attempt = await db_session.scalar(
        select(AgentRunAttempt).where(
            AgentRunAttempt.run_id == run.id, AgentRunAttempt.attempt == 1
        )
    )
    if attempt is None:
        attempt = AgentRunAttempt(
            id=str(uuid4()),
            run_id=run.id,
            attempt=1,
            started_at=now,
            outcome="completed",
        )
        db_session.add(attempt)
        await db_session.flush()
    sequence = await db_session.scalar(
        select(func.max(AgentStep.sequence)).where(AgentStep.run_id == run.id)
    )
    step = AgentStep(
        id=str(uuid4()),
        run_id=run.id,
        attempt_id=attempt.id,
        sequence=(sequence or 0) + 1,
        step_type="tool_call",
        status="completed",
        visibility="internal",
        created_at=now,
    )
    db_session.add(step)
    await db_session.flush()
    call = AgentToolCall(
        id=str(uuid4()),
        run_id=run.id,
        step_id=step.id,
        logical_call_id=str(uuid4()),
        service="internal",
        internal_tool_name="marketing_evidence",
        arguments_json={},
        arguments_hash="evidence-args",
        status="settled",
        started_at=now,
        completed_at=now,
    )
    db_session.add(call)
    await db_session.flush()
    evidence = EvidenceItem(
        id=evidence_id,
        session_id=run.session_id,
        run_id=run.id,
        tool_call_id=call.id,
        source_type="mcp",
        source_name="test",
        raw_payload_json=payload,
        payload_hash="evidence-hash",
        collected_at=now,
        availability_status="available",
    )
    db_session.add(evidence)
    await db_session.flush()
    return evidence.id


def _invalid_brand_payload() -> dict[str, Any]:
    payload = deepcopy(build_brand_dict())
    payload["data_status"] = "restricted"
    payload["data"]["overview"]["total_volume"] = None
    payload["availability"]["overview"] = {
        "status": "partial",
        "reason_codes": ["volume_unavailable"],
    }
    payload["limitations"] = [
        {
            "code": "volume_unavailable",
            "message": "声量不可用",
            "affected_paths": ["data.overview.total_volume"],
        }
    ]
    for field in payload["canonical_data"]:
        if field["path"] == "/data/overview/total_volume":
            field["value"] = None
            field["availability"] = "unavailable"
            field["evidence_ids"] = []
    return payload


def _legal_brand_payload() -> dict[str, Any]:
    payload = _invalid_brand_payload()
    payload["data_status"] = "complete"
    payload["data"]["overview"]["total_volume"] = 100
    payload["availability"]["overview"] = {"status": "complete", "reason_codes": []}
    payload["limitations"] = []
    for field in payload["canonical_data"]:
        if field["path"] == "/data/overview/total_volume":
            field["value"] = 100
            field["availability"] = "complete"
    return payload


def _refs_for_numeric_leaves(payload: dict[str, Any], evidence_id: str) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for path, value in walk_data_leaves(payload["data"]):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        refs.append(
            {
                "artifact_path": path,
                "sources": [
                    {
                        "source_type": "evidence",
                        "evidence_id": evidence_id,
                        "source_path": path.removeprefix("/data"),
                    }
                ],
            }
        )
    return refs


@pytest.mark.asyncio
async def test_direct_publish_blocks_unavailable_narrative_claim(
    db_session, user_factory, session_factory, run_factory
) -> None:
    """红灯：当前直接发布仍接受 narrative 指向 unavailable canonical 的 payload。"""
    user = await user_factory()
    session = await session_factory(user.id)
    run = await run_factory(session.id, user.id)
    await AgentRunRepository(db_session).claim_lease(run.id, WORKER, 300)

    payload = _invalid_brand_payload()
    evidence_id = await _write_evidence(db_session, run, payload["data"])
    refs = _refs_for_numeric_leaves(payload, evidence_id)
    # 使用实际 Evidence ID，避免红灯被 fixture ID 误导。
    for field in payload["canonical_data"]:
        if field["value"] is not None and isinstance(field["value"], (int, float)):
            field["evidence_ids"] = [evidence_id]

    _, draft, _revision = await ArtifactService(db_session).create_or_get_draft(
        session_id=session.id,
        user_id=user.id,
        run_id=run.id,
        module="brand",
        business_fields={"brand": "某品牌"},
        schema_version="brand_report_v3",
        artifact_type="brand_report_v3",
        payload=payload,
        evidence_refs=refs,
    )

    result = await ArtifactPublicationService(db_session).publish(
        run_id=run.id,
        draft_ids=(draft.id,),
        worker_id=WORKER,
    )

    assert result[0].status == "validation_failed"
    assert await db_session.scalar(select(AgentArtifactVersion.id)) is None
    attempt = await db_session.scalar(
        select(ArtifactPublishAttempt).where(ArtifactPublishAttempt.draft_revision_id == _revision.id)
    )
    assert attempt is not None
    structured_stage = attempt.validation_json["stages"]["structured_claims"]
    assert structured_stage["status"] == "evaluated"
    assert structured_stage["valid"] is False


@pytest.mark.asyncio
async def test_direct_publish_marks_structured_claims_not_evaluated_after_lineage_failure(
    db_session, user_factory, session_factory, run_factory, monkeypatch
) -> None:
    user = await user_factory()
    session = await session_factory(user.id)
    run = await run_factory(session.id, user.id)
    await AgentRunRepository(db_session).claim_lease(run.id, WORKER, 300)
    payload = _legal_brand_payload()
    evidence_id = await _write_evidence(db_session, run, payload["data"])
    refs = _refs_for_numeric_leaves(payload, evidence_id)
    # 删除一个最终 numeric leaf 的 lineage，令 freezer 先失败；canonical contract 仍完整。
    refs.pop()
    for field in payload["canonical_data"]:
        if field["value"] is not None and isinstance(field["value"], (int, float)):
            field["evidence_ids"] = [evidence_id]
    _, draft, revision = await ArtifactService(db_session).create_or_get_draft(
        session_id=session.id,
        user_id=user.id,
        run_id=run.id,
        module="brand",
        business_fields={"brand": "某品牌"},
        schema_version="brand_report_v3",
        artifact_type="brand_report_v3",
        payload=payload,
        evidence_refs=refs,
    )
    called = False

    def fail_if_evaluated(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("structured claims must not run after lineage failure")

    monkeypatch.setattr(publishing_module, "validate_structured_claims", fail_if_evaluated)
    result = await ArtifactPublicationService(db_session).publish(
        run_id=run.id, draft_ids=(draft.id,), worker_id=WORKER
    )
    assert result[0].status == "validation_failed"
    assert called is False
    attempt = await db_session.scalar(
        select(ArtifactPublishAttempt).where(ArtifactPublishAttempt.draft_revision_id == revision.id)
    )
    assert attempt is not None
    structured_stage = attempt.validation_json["stages"]["structured_claims"]
    assert structured_stage["status"] == "not_evaluated"
    assert structured_stage.get("valid") is not True


@pytest.mark.asyncio
async def test_direct_publish_uses_same_candidate_version_for_claims_and_persistence(
    db_session, user_factory, session_factory, run_factory, monkeypatch
) -> None:
    user = await user_factory()
    session = await session_factory(user.id)
    run = await run_factory(session.id, user.id)
    await AgentRunRepository(db_session).claim_lease(run.id, WORKER, 300)
    payload = _legal_brand_payload()
    evidence_id = await _write_evidence(db_session, run, payload["data"])
    refs = _refs_for_numeric_leaves(payload, evidence_id)
    for field in payload["canonical_data"]:
        if field["value"] is not None and isinstance(field["value"], (int, float)):
            field["evidence_ids"] = [evidence_id]
    _, draft, _revision = await ArtifactService(db_session).create_or_get_draft(
        session_id=session.id,
        user_id=user.id,
        run_id=run.id,
        module="brand",
        business_fields={"brand": "某品牌"},
        schema_version="brand_report_v3",
        artifact_type="brand_report_v3",
        payload=payload,
        evidence_refs=refs,
    )
    captured: list[str] = []
    original = publishing_module.validate_structured_claims

    def capture(candidate_payload, candidate_id, scope):
        captured.append(candidate_id)
        return original(candidate_payload, candidate_id, scope)

    monkeypatch.setattr(publishing_module, "validate_structured_claims", capture)
    result = await ArtifactPublicationService(db_session).publish(
        run_id=run.id, draft_ids=(draft.id,), worker_id=WORKER
    )
    assert result[0].status == "published"
    assert captured == [result[0].artifact_version_id]


@pytest.mark.asyncio
async def test_direct_publish_accepts_legal_brand_campaign_and_preserves_idempotency(
    db_session, user_factory, session_factory, run_factory
) -> None:
    from tests.agent_artifacts.test_payloads import build_campaign_dict

    user = await user_factory()
    session = await session_factory(user.id)
    run = await run_factory(session.id, user.id)
    await AgentRunRepository(db_session).claim_lease(run.id, WORKER, 300)
    service = ArtifactService(db_session)
    handles = []
    for module, payload, business_fields, evidence_id in (
        ("brand", _legal_brand_payload(), {"brand": "某品牌"}, "ev-brand"),
        (
            "campaign",
            build_campaign_dict(),
            {"brand": "某品牌", "campaign": "C1"},
            "ev-campaign",
        ),
    ):
        evidence_id = await _write_evidence(
            db_session, run, payload["data"], evidence_id=evidence_id
        )
        refs = _refs_for_numeric_leaves(payload, evidence_id)
        for field in payload["canonical_data"]:
            if field["value"] is not None and isinstance(field["value"], (int, float)):
                field["evidence_ids"] = [evidence_id]
        artifact, draft, _revision = await service.create_or_get_draft(
            session_id=session.id,
            user_id=user.id,
            run_id=run.id,
            module=module,
            business_fields=business_fields,
            schema_version=("brand_report_v3" if module == "brand" else "campaign_report_v2"),
            artifact_type=("brand_report_v3" if module == "brand" else "campaign_report_v2"),
            payload=payload,
            evidence_refs=refs,
        )
        handles.append((artifact, draft))

    publication = ArtifactPublicationService(db_session)
    first = await publication.publish(
        run_id=run.id,
        draft_ids=tuple(draft.id for _artifact, draft in handles),
        worker_id=WORKER,
    )
    assert [item.status for item in first] == ["published", "published"]
    replay = await publication.publish(
        run_id=run.id, draft_ids=(handles[0][1].id,), worker_id=WORKER
    )
    assert replay[0].artifact_version_id == first[0].artifact_version_id
    assert await db_session.scalar(select(AgentArtifactVersion.id)) is not None


@pytest.mark.asyncio
async def test_publish_batch_blocks_the_same_structured_claims(
    db_session, user_factory, session_factory, run_factory
) -> None:
    user = await user_factory()
    session = await session_factory(user.id)
    run = await run_factory(session.id, user.id)
    repo = AgentRunRepository(db_session)
    await repo.claim_lease(run.id, WORKER, 300)
    await repo.transition(run.id, RunStatus.REVIEWING, worker_id=WORKER)

    payload = _invalid_brand_payload()
    evidence_id = await _write_evidence(db_session, run, payload["data"])
    refs = _refs_for_numeric_leaves(payload, evidence_id)
    for field in payload["canonical_data"]:
        if field["value"] is not None and isinstance(field["value"], (int, float)):
            field["evidence_ids"] = [evidence_id]
    _, draft, revision = await ArtifactService(db_session).create_or_get_draft(
        session_id=session.id,
        user_id=user.id,
        run_id=run.id,
        module="brand",
        business_fields={"brand": "某品牌"},
        schema_version="brand_report_v3",
        artifact_type="brand_report_v3",
        payload=payload,
        evidence_refs=refs,
    )
    batch = ArtifactReviewBatch(
        id=str(uuid4()),
        parent_run_id=run.id,
        status="pending",
        completion_text="完成",
        created_at=utc_now(),
    )
    db_session.add(batch)
    await db_session.flush()
    db_session.add(
        ArtifactReviewItem(
            id=str(uuid4()),
            batch_id=batch.id,
            artifact_id=draft.artifact_id,
            draft_revision_id=revision.id,
            status="approved",
        )
    )
    await db_session.flush()

    with pytest.raises(PublishBlocked) as exc_info:
        await ArtifactService(db_session).publish_batch(batch.id, worker_id=WORKER)

    assert any(issue["code"] == "supporting_path_unavailable" for issue in exc_info.value.issues)
    assert await db_session.scalar(select(AgentArtifactVersion.id)) is None
