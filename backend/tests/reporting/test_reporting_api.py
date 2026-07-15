from sqlalchemy import select

from app.identity.models import AuthIdentity
from app.reporting.service import ReportingService
from tests.reporting.fakes import candidate_fixture, completed_task_factory


async def _user_id(db_session, phone: str) -> str:
    return await db_session.scalar(
        select(AuthIdentity.user_id).where(
            AuthIdentity.provider == "sms", AuthIdentity.provider_subject == phone
        )
    )


async def test_candidates_are_sortable_and_report_matches_candidate_version(
    auth_client_factory, db_session
) -> None:
    client = await auth_client_factory("13800000101")
    user_id = await _user_id(db_session, "13800000101")
    task = await completed_task_factory(
        db_session,
        user_id,
        evidence_rows=[
            candidate_fixture(account_id="100", engagement_score=60),
            candidate_fixture(account_id="200", engagement_score=90),
        ],
    )
    service = ReportingService(db_session)
    await service.build_candidate_version(task.id, "balanced")
    report = await service.build_bi_report(task.id)

    candidates = await client.get(
        f"/api/v1/tasks/{task.id}/candidates",
        params={"sort": "engagement", "direction": "desc"},
    )
    report_response = await client.get(f"/api/v1/reports/{report.id}")

    assert candidates.status_code == 200
    body = candidates.json()
    assert body["items"][0]["scores"]["engagement"] >= body["items"][1]["scores"]["engagement"]
    assert report_response.status_code == 200
    assert report_response.json()["candidate_version"] == body["version"]


async def test_candidates_and_reports_are_hidden_from_other_users(
    auth_client_factory, db_session
) -> None:
    alice = await auth_client_factory("13800000111")
    bob = await auth_client_factory("13800000112")
    user_id = await _user_id(db_session, "13800000111")
    task = await completed_task_factory(
        db_session, user_id, evidence_rows=[candidate_fixture(account_id="100")]
    )
    service = ReportingService(db_session)
    await service.build_candidate_version(task.id, "balanced")
    report = await service.build_bi_report(task.id)

    assert (await bob.get(f"/api/v1/tasks/{task.id}/candidates")).status_code == 404
    assert (await bob.get(f"/api/v1/reports/{report.id}")).status_code == 404
    assert (await alice.get(f"/api/v1/reports/{report.id}")).status_code == 200
