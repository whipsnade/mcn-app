from sqlalchemy import select

from app.identity.models import AuthIdentity
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
