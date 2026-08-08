"""Task 5: KOL scope 与候选发布有效性门禁。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.agent_artifacts.builders.kol_selection import build_kol_selection_draft
from app.agent_artifacts.exporters import ArtifactExportUnsupported, export_artifact
from app.agent_artifacts.models import (
    AgentArtifactVersion,
    ArtifactReviewBatch,
    ArtifactReviewItem,
)
from app.agent_artifacts.publishing import ArtifactPublicationService
from app.agent_artifacts.service import ArtifactService
from app.agent_artifacts.validation import ArtifactPayloadInvalid, validate_kol_candidates
from app.agent_runtime.repository import AgentRunRepository
from app.agent_runtime.tools.contracts import ToolContext
from tests.agent_artifacts.test_payloads import (
    build_kol_selection_dict,
    build_kol_value_selection_dict,
)


def _valid_payload() -> dict:
    payload = build_kol_value_selection_dict()
    payload["scope"].update(
        {
            "region": ["上海"],
            "age_range": ["25-34"],
            "period": {"start": "2026-01-01", "end": "2026-01-31", "timezone": "Asia/Shanghai"},
            "budget": None,
            "ranking_mode": "balanced",
            "top_limit": 20,
            "scoring_version": "kol_value_score_v3",
        }
    )
    quote = payload["data"]["items"][0]["quoted_price"]
    payload["data"]["items"][0]["score_snapshot"]["quoted_price"] = quote
    return payload


def _codes(payload: dict) -> set[str]:
    return {issue.code for issue in validate_kol_candidates(payload)}


def test_legal_kol_selection_passes_candidate_gate() -> None:
    assert validate_kol_candidates(_valid_payload()) == []


def test_scope_v3_is_persisted_by_builder_compatible_defaults() -> None:
    scope = _valid_payload()["scope"]
    for field in (
        "brand",
        "category",
        "platforms",
        "audience",
        "region",
        "age_range",
        "period",
        "budget",
        "filters",
        "ranking_mode",
        "top_limit",
        "scoring_version",
    ):
        assert field in scope


@pytest.mark.parametrize(
    ("path", "value", "code"),
    [
        (("data", "items", 0, "nickname"), "   ", "kol_nickname_missing"),
        (("data", "items", 0, "platform"), "unknown", "kol_platform_invalid"),
        (("data", "items", 0, "kol_uid"), "", "kol_identity_missing"),
    ],
)
def test_invalid_candidate_identity_and_platform_fail_closed(path, value, code) -> None:
    payload = _valid_payload()
    target = payload
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    assert code in _codes(payload)


def test_candidate_with_all_scores_missing_is_not_publishable() -> None:
    payload = _valid_payload()
    snapshot = payload["data"]["items"][0]["score_snapshot"]
    for dimension in snapshot["dimensions"].values():
        dimension["raw_score"] = 0.0
        dimension["weighted_score"] = 0.0
        dimension["source"] = None
        dimension["missing_reason"] = "missing_input"
    assert "kol_scores_missing" in _codes(payload)


def test_missing_scope_or_scope_field_is_rejected() -> None:
    payload = _valid_payload()
    payload.pop("scope")
    assert "kol_scope_missing" in _codes(payload)

    payload = _valid_payload()
    payload["scope"].pop("platforms")
    assert "kol_scope_incomplete" in _codes(payload)


def test_narrative_cannot_name_outsider() -> None:
    payload = _valid_payload()
    payload["narrative"]["fit_findings"][0]["kol_uid"] = "outsider"
    assert "kol_narrative_outsider" in _codes(payload)


def test_budget_and_quote_must_remain_within_scope() -> None:
    payload = _valid_payload()
    payload["scope"]["budget"] = 1000
    payload["data"]["items"][0]["quoted_price"] = 1001
    payload["data"]["items"][0]["score_snapshot"]["quoted_price"] = 1001
    assert "kol_budget_untraceable" in _codes(payload)


def test_v3_quote_without_snapshot_trace_is_rejected() -> None:
    payload = _valid_payload()
    payload["data"]["items"][0]["quoted_price"] = 999
    payload["data"]["items"][0]["score_snapshot"]["quoted_price"] = None
    assert "kol_quote_untraceable" in _codes(payload)


def test_empty_items_are_gaps_not_formally_publishable() -> None:
    payload = _valid_payload()
    payload["data"]["items"] = []
    payload["data"]["summary"]["selected_count"] = None
    payload["data_status"] = "restricted"
    payload["availability"]["items"] = {
        "status": "unavailable",
        "reason_codes": ["no_kol_data"],
    }
    payload["availability"]["summary"] = {
        "status": "unavailable",
        "reason_codes": ["no_kol_data"],
    }
    payload["limitations"] = [
        {
            "code": "insufficient_kol_data",
            "message": "候选不足",
            "affected_paths": ["data.items", "data.summary"],
        }
    ]
    assert "kol_empty_items" in _codes(payload)


def test_historical_v2_payload_remains_readable() -> None:
    assert validate_kol_candidates(build_kol_selection_dict()) == []


async def test_builder_does_not_turn_stable_id_into_missing_nickname() -> None:
    scope = {
        "category": "美食",
        "platforms": ["xiaohongshu"],
        "audience": {"regions": ["上海"], "age_ranges": ["25-34"], "interests": ["咖啡"]},
        "filters": {},
    }
    row = {
        "platform": "xiaohongshu",
        "kol_uid": "stable-kol-id",
        "followers": 1000,
        "avg_engagement": 100,
        "engagement_total": 100,
        "active_followers": 500,
        "active_follower_rate": 50,
        "score_inputs": {
            "average_interactions": 100,
            "active_follower_rate": 50,
            "interaction_follower_ratio": 10,
            "followers": 1000,
            "content_score": 50,
            "industry_interest": 50,
            "target_region": 50,
            "target_age": 50,
        },
    }
    result = await build_kol_selection_draft(
        scope=scope,
        evidence_id="ev-builder",
        items=[row],
        context=ToolContext(
            user_id="u",
            session_id="s",
            run_id="r",
            profile_name="session_analyst_v1",
        ),
    )
    item = result.payload["data"]["items"][0]
    assert item["kol_uid"] == "stable-kol-id"
    assert item["nickname"] == ""
    assert "kol_nickname_missing" in _codes(result.payload)


class _Version:
    def __init__(self, payload: dict, *, status: str = "published", validation_json=None):
        self.schema_version = "kol_selection_v3"
        self.payload_json = payload
        self.status = status
        self.validation_json = validation_json


def test_exporter_rejects_unpublished_or_failed_validity_version() -> None:
    with pytest.raises(ArtifactExportUnsupported):
        export_artifact(_Version(_valid_payload(), status="draft"))
    with pytest.raises(ArtifactExportUnsupported):
        export_artifact(_Version(_valid_payload(), validation_json={"valid": False}))


def test_exporter_accepts_published_legal_and_history_versions() -> None:
    assert export_artifact(_Version(_valid_payload()))[:2] == b"PK"
    assert export_artifact(_Version(build_kol_selection_dict()))[:2] == b"PK"


async def _persist_kol_draft(db_session, user_id: str, session_id: str, run_id: str):
    payload = _valid_payload()
    return await ArtifactService(db_session).create_or_get_draft(
        session_id=session_id,
        user_id=user_id,
        run_id=run_id,
        module="kol-selection",
        business_fields={"scope": payload["scope"]},
        schema_version="kol_selection_v3",
        artifact_type="kol_selection_v3",
        payload=payload,
        evidence_refs=[],
    )


async def test_direct_publish_cannot_bypass_kol_candidate_gate(
    db_session, user_factory, session_factory, run_factory
) -> None:
    user = await user_factory()
    session = await session_factory(user.id)
    run = await run_factory(session.id, user.id)
    assert await AgentRunRepository(db_session).claim_lease(run.id, "worker", 300)
    _artifact, draft, revision = await _persist_kol_draft(
        db_session, user.id, session.id, run.id
    )
    payload = dict(revision.payload_json)
    payload["data"] = dict(payload["data"])
    payload["data"]["items"] = [dict(payload["data"]["items"][0])]
    payload["data"]["items"][0]["nickname"] = ""
    revision.payload_json = payload
    await db_session.flush()

    results = await ArtifactPublicationService(db_session).publish(
        run_id=run.id, draft_ids=(draft.id,), worker_id="worker"
    )
    assert results[0].status == "validation_failed"
    assert any(error.get("code") == "kol_nickname_missing" for error in results[0].errors)
    assert not any(
        await db_session.scalars(
            select(AgentArtifactVersion).where(
                AgentArtifactVersion.source_draft_revision_id == revision.id
            )
        )
    )


async def test_batch_publish_cannot_bypass_kol_candidate_gate(
    db_session, user_factory, session_factory, run_factory
) -> None:
    user = await user_factory()
    session = await session_factory(user.id)
    run = await run_factory(session.id, user.id)
    assert await AgentRunRepository(db_session).claim_lease(run.id, "worker", 300)
    artifact, _draft, revision = await _persist_kol_draft(
        db_session, user.id, session.id, run.id
    )
    payload = dict(revision.payload_json)
    payload["data"] = dict(payload["data"])
    payload["data"]["items"] = [dict(payload["data"]["items"][0])]
    payload["data"]["items"][0]["platform"] = "unknown"
    revision.payload_json = payload
    now = datetime.now(UTC).replace(tzinfo=None)
    batch = ArtifactReviewBatch(
        id=str(uuid4()),
        parent_run_id=run.id,
        status="pending",
        completion_text="完成",
        created_at=now,
    )
    item = ArtifactReviewItem(
        id=str(uuid4()),
        batch_id=batch.id,
        artifact_id=artifact.id,
        draft_revision_id=revision.id,
        status="approved",
    )
    db_session.add_all([batch, item])
    await db_session.flush()

    with pytest.raises(ArtifactPayloadInvalid) as excinfo:
        await ArtifactService(db_session).publish_batch(batch.id, worker_id="worker")
    assert any(error.get("code") == "kol_platform_invalid" for error in excinfo.value.errors)
    assert not any(
        await db_session.scalars(
            select(AgentArtifactVersion).where(AgentArtifactVersion.artifact_id == artifact.id)
        )
    )
