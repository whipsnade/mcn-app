from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.identity.models import User
from app.marketing_skills.models import SkillActivation, SkillRevision
from app.marketing_skills.repository import (
    resolve_active_revisions,
    stable_rollout_bucket,
)
from app.tenancy.models import Tenant
from app.marketing_skills.validation import canonical_skill_digest


@pytest.fixture
async def skill_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=[
                    User.__table__,
                    Tenant.__table__,
                    SkillRevision.__table__,
                    SkillActivation.__table__,
                ],
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _revision(*, name: str, content: str, revision: int, tenant_id: str | None = None) -> SkillRevision:
    return SkillRevision(
        id=str(uuid4()),
        tenant_id=tenant_id,
        skill_name=name,
        revision=revision,
        content=content,
        content_digest=canonical_skill_digest(content),
        description=f"{name} description",
        required_tools=["datatap.search_campaign"],
        artifact_contract="analysis_report_v1",
        created_by=None,
        created_at=_now(),
        change_note="test",
    )


@pytest.mark.asyncio
async def test_resolve_active_revisions_prefers_tenant_and_falls_back_to_global(skill_session) -> None:
    tenant_id = str(uuid4())
    global_revision = _revision(name="campaign-research", content="global", revision=1)
    tenant_revision = _revision(
        name="campaign-research",
        content="tenant",
        revision=2,
        tenant_id=tenant_id,
    )
    skill_session.add_all(
        [
            User(
                id=str(uuid4()),
                nickname="admin",
                role="admin",
                status="active",
                industries=[],
                created_at=_now(),
                updated_at=_now(),
            ),
            Tenant(
                id=tenant_id,
                slug="tenant",
                name="Tenant",
                status="active",
                is_internal=False,
                runtime_backend="pi",
                license_status="active",
                created_at=_now(),
                updated_at=_now(),
            ),
            global_revision,
            tenant_revision,
            SkillActivation(
                id=str(uuid4()),
                environment="production",
                tenant_id=None,
                skill_name="campaign-research",
                active_revision_id=global_revision.id,
                previous_revision_id=None,
                rollout_percent=100,
                updated_by=None,
                updated_at=_now(),
            ),
            SkillActivation(
                id=str(uuid4()),
                environment="production",
                tenant_id=tenant_id,
                skill_name="campaign-research",
                active_revision_id=tenant_revision.id,
                previous_revision_id=None,
                rollout_percent=100,
                updated_by=None,
                updated_at=_now(),
            ),
        ]
    )
    await skill_session.commit()

    resolved = await resolve_active_revisions(
        skill_session,
        tenant_id=tenant_id,
        skill_names=["campaign-research"],
    )

    assert [item.content for item in resolved] == ["tenant"]
    assert resolved[0].revision == 2

    await skill_session.execute(
        SkillActivation.__table__.update()
        .where(SkillActivation.tenant_id == tenant_id)
        .values(rollout_percent=0)
    )
    await skill_session.commit()

    fallback = await resolve_active_revisions(
        skill_session,
        tenant_id=tenant_id,
        skill_names=["campaign-research"],
    )

    assert [item.content for item in fallback] == ["global"]


@pytest.mark.asyncio
async def test_rollout_bucket_is_stable_and_revision_is_immutable(skill_session) -> None:
    tenant_id = str(uuid4())
    content = "stable"
    revision = _revision(name="campaign-research", content=content, revision=1)
    skill_session.add(revision)
    await skill_session.commit()

    assert stable_rollout_bucket(tenant_id, "campaign-research") == stable_rollout_bucket(
        tenant_id, "campaign-research"
    )

    revision.description = "mutated"
    with pytest.raises(ValueError, match="skill_revision_immutable"):
        await skill_session.commit()
    await skill_session.rollback()


@pytest.mark.asyncio
async def test_rollout_zero_uses_previous_revision_for_same_tenant(skill_session) -> None:
    tenant_id = str(uuid4())
    previous = _revision(name="campaign-research", content="previous", revision=1)
    active = _revision(name="campaign-research", content="active", revision=2)
    skill_session.add_all(
        [
            previous,
            active,
            SkillActivation(
                id=str(uuid4()),
                environment="production",
                tenant_id=tenant_id,
                skill_name="campaign-research",
                active_revision_id=active.id,
                previous_revision_id=previous.id,
                rollout_percent=0,
                updated_by=None,
                updated_at=_now(),
            ),
        ]
    )
    await skill_session.commit()

    resolved = await resolve_active_revisions(
        skill_session,
        tenant_id=tenant_id,
        skill_names=["campaign-research"],
    )

    assert [item.content for item in resolved] == ["previous"]


@pytest.mark.asyncio
async def test_rollout_zero_tenant_does_not_drop_base_global_revision(skill_session) -> None:
    tenant_id = str(uuid4())
    global_revision = _revision(name="campaign-research", content="global", revision=1)
    tenant_active = _revision(
        name="campaign-research", content="tenant-active", revision=1, tenant_id=tenant_id
    )
    skill_session.add_all(
        [
            global_revision,
            tenant_active,
            SkillActivation(
                id=str(uuid4()),
                environment="production",
                tenant_id=None,
                skill_name="campaign-research",
                active_revision_id=global_revision.id,
                previous_revision_id=None,
                rollout_percent=100,
                updated_by=None,
                updated_at=_now(),
            ),
            SkillActivation(
                id=str(uuid4()),
                environment="production",
                tenant_id=tenant_id,
                skill_name="campaign-research",
                active_revision_id=tenant_active.id,
                previous_revision_id=None,
                rollout_percent=0,
                updated_by=None,
                updated_at=_now(),
            ),
        ]
    )
    await skill_session.commit()

    resolved = await resolve_active_revisions(
        skill_session,
        tenant_id=tenant_id,
        skill_names=["campaign-research"],
    )

    assert [item.content for item in resolved] == ["global"]
