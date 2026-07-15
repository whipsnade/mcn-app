from sqlalchemy import select

from app.identity.models import AuthIdentity
from app.mcp_gateway.models import McpCall
from app.reporting.service import ReportingService
from tests.reporting.fakes import candidate_fixture, completed_task_factory


async def test_session_restore_includes_only_owned_latest_analysis(
    auth_client_factory, db_session
) -> None:
    alice = await auth_client_factory("13800000131")
    bob = await auth_client_factory("13800000132")
    user_id = await db_session.scalar(
        select(AuthIdentity.user_id).where(
            AuthIdentity.provider == "sms", AuthIdentity.provider_subject == "13800000131"
        )
    )
    task = await completed_task_factory(
        db_session, user_id, evidence_rows=[candidate_fixture(account_id="restore-100")]
    )
    service = ReportingService(db_session)
    candidate_version = await service.build_candidate_version(task.id, "balanced")
    report = await service.build_bi_report(task.id)

    restored = await alice.get(f"/api/v1/sessions/{task.session_id}")

    assert restored.status_code == 200
    body = restored.json()
    assert body["latest_task"]["id"] == task.id
    assert body["latest_candidates"]["version"] == candidate_version.candidate_version
    assert body["latest_report"]["id"] == report.id
    assert body["latest_candidates"]["total"] == 1
    assert (await bob.get(f"/api/v1/sessions/{task.session_id}")).status_code == 404


async def test_session_restore_never_pairs_latest_candidates_with_older_report(
    auth_client_factory, db_session
) -> None:
    alice = await auth_client_factory("13800000133")
    user_id = await db_session.scalar(
        select(AuthIdentity.user_id).where(
            AuthIdentity.provider == "sms", AuthIdentity.provider_subject == "13800000133"
        )
    )
    task = await completed_task_factory(
        db_session, user_id, evidence_rows=[candidate_fixture(account_id="versioned-100")]
    )
    service = ReportingService(db_session)
    first = await service.build_candidate_version(task.id, "balanced")
    await service.build_bi_report(task.id)
    call = await db_session.scalar(select(McpCall).where(McpCall.task_id == task.id))
    call.evidence_json["structured_content"]["engagement_score"] = 91
    second = await service.build_candidate_version(task.id, "balanced")

    restored = await alice.get(f"/api/v1/sessions/{task.session_id}")

    assert second.candidate_version == first.candidate_version + 1
    assert restored.json()["latest_candidates"]["version"] == second.candidate_version
    assert restored.json()["latest_report"] is None
