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


async def test_favorite_is_user_owned_and_idempotent(auth_client_factory, db_session) -> None:
    alice = await auth_client_factory("13800000121")
    bob = await auth_client_factory("13800000122")
    user_id = await _user_id(db_session, "13800000121")
    task = await completed_task_factory(
        db_session, user_id, evidence_rows=[candidate_fixture(account_id="favorite-100")]
    )
    candidate = (await ReportingService(db_session).build_candidate_version(task.id, "balanced")).candidates[0]

    first = await alice.post(
        "/api/v1/favorites", json={"kol_id": candidate.kol_id, "source_task_id": task.id}
    )
    second = await alice.post(
        "/api/v1/favorites", json={"kol_id": candidate.kol_id, "note": "重点关注"}
    )

    assert first.status_code in {200, 201}
    assert second.status_code == 200
    assert second.json()["kol_id"] == first.json()["kol_id"]
    assert second.json()["note"] == "重点关注"
    assert (await bob.get("/api/v1/favorites")).json() == []
    assert (await bob.delete(f"/api/v1/favorites/{candidate.kol_id}")).status_code == 404
    assert (await alice.delete(f"/api/v1/favorites/{candidate.kol_id}")).status_code == 204
    assert (await alice.get("/api/v1/favorites")).json() == []
