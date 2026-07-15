import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import engine
from app.identity.models import AuthIdentity
from app.identity.models import User
from app.reporting.models import Kol
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


async def test_concurrent_favorite_creates_converge_to_one_user_kol_pair() -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    user_id = str(uuid4())
    kol_id = str(uuid4())
    async with AsyncSession(engine, expire_on_commit=False) as seed:
        seed.add(
            User(
                id=user_id,
                nickname="并发收藏用户",
                role="user",
                status="active",
                created_at=now,
                updated_at=now,
            )
        )
        seed.add(
            Kol(
                id=kol_id,
                platform="bilibili",
                platform_account_id=f"concurrent-{kol_id}",
                normalized_profile_url=None,
                created_at=now,
                updated_at=now,
            )
        )
        await seed.commit()

    async def create(note: str):
        async with AsyncSession(engine, expire_on_commit=False) as session:
            result = await ReportingService(session).create_favorite(
                user_id, kol_id=kol_id, note=note, source_task_id=None
            )
            await session.commit()
            return result

    try:
        first, second = await asyncio.gather(create("首次"), create("并发"))
        async with AsyncSession(engine) as session:
            favorites = await ReportingService(session).list_favorites(user_id)
        assert first[0].kol_id == second[0].kol_id == kol_id
        assert len(favorites) == 1
    finally:
        async with AsyncSession(engine) as cleanup:
            await cleanup.execute(delete(User).where(User.id == user_id))
            await cleanup.execute(delete(Kol).where(Kol.id == kol_id))
            await cleanup.commit()
