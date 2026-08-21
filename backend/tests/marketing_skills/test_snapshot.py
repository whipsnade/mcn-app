from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.identity.models import User
from app.marketing_capability_pack.runtime import build_marketing_run_capability
from app.marketing_skills.models import SkillActivation, SkillRevision
from app.marketing_skills.repository import ResolvedSkillRevision
from app.marketing_skills.snapshot import (
    SkillManifest,
    SkillSnapshotService,
)
from app.marketing_skills.validation import canonical_skill_digest
from app.tenancy.models import Tenant


@pytest_asyncio.fixture
async def snapshot_session():
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


def _resolved(name: str = "campaign-research", revision: int = 7) -> ResolvedSkillRevision:
    content = f"""---
name: {name}
description: 数据分析 Skill
required_tools: []
---

根据真实数据分析，不绕过权限或计费。
"""
    return ResolvedSkillRevision(
        id=str(uuid4()),
        tenant_id=None,
        skill_name=name,
        revision=revision,
        content=content,
        content_digest=canonical_skill_digest(content),
        description="数据分析 Skill",
        required_tools=(),
        artifact_contract="analysis_report_v1",
    )


def test_skill_manifest_is_immutable_and_digest_bound() -> None:
    manifest = SkillManifest.from_revisions((_resolved(),))

    assert manifest.entries[0].revision == 7
    assert manifest.entries[0].content_digest == canonical_skill_digest(
        manifest.entries[0].content
    )
    with pytest.raises((ValidationError, TypeError)):
        manifest.entries = ()

    tampered = manifest.model_dump(mode="json")
    tampered["entries"][0]["content"] += "\nchanged"
    with pytest.raises(ValidationError, match="skill_snapshot_digest_mismatch"):
        SkillManifest.model_validate(tampered)


def test_skill_manifest_rejects_multibyte_content_over_gateway_limit() -> None:
    content = "---\nname: campaign-research\ndescription: 数据分析\nrequired_tools: []\n---\n" + "中" * 70_000
    resolved = ResolvedSkillRevision(
        id=str(uuid4()),
        tenant_id=None,
        skill_name="campaign-research",
        revision=1,
        content=content,
        content_digest=canonical_skill_digest(content),
        description="数据分析",
        required_tools=(),
        artifact_contract=None,
    )

    with pytest.raises(ValidationError, match="skill_snapshot_content_too_large"):
        SkillManifest.from_revisions((resolved,))


@pytest.mark.asyncio
async def test_new_run_resolution_uses_active_db_revision_and_never_static_content(
    snapshot_session,
) -> None:
    tenant_id = str(uuid4())
    resolved = _resolved()
    now = _now()
    snapshot_session.add_all(
        [
            User(
                id=str(uuid4()),
                nickname="user",
                role="user",
                status="active",
                industries=[],
                created_at=now,
                updated_at=now,
            ),
            Tenant(
                id=tenant_id,
                slug="snapshot-tenant",
                name="Snapshot Tenant",
                status="active",
                is_internal=False,
                runtime_backend="pi",
                license_status="active",
                created_at=now,
                updated_at=now,
            ),
            SkillRevision(
                id=resolved.id,
                tenant_id=None,
                skill_name=resolved.skill_name,
                revision=resolved.revision,
                content=resolved.content,
                content_digest=resolved.content_digest,
                description=resolved.description,
                required_tools=[],
                artifact_contract=resolved.artifact_contract,
                created_by=None,
                created_at=now,
                change_note="test",
            ),
            SkillActivation(
                id=str(uuid4()),
                environment="production",
                tenant_id=None,
                skill_name=resolved.skill_name,
                active_revision_id=resolved.id,
                previous_revision_id=None,
                rollout_percent=100,
                updated_by=None,
                updated_at=now,
            ),
        ]
    )
    await snapshot_session.commit()

    base = build_marketing_run_capability()
    resolved_capability = await SkillSnapshotService.resolve_for_new_run(
        snapshot_session,
        tenant_id=tenant_id,
        base_capability=base,
    )
    assert {item.name for item in resolved_capability.skills} == {
        item.name for item in base.skills
    } | {"campaign-research"}
    campaign = next(
        item for item in resolved_capability.skills if item.name == "campaign-research"
    )
    assert campaign.revision == 7
    assert campaign.content == resolved.content


@pytest.mark.asyncio
async def test_new_run_resolution_preserves_base_skills_when_registry_is_partial(
    snapshot_session,
):
    tenant_id = str(uuid4())
    resolved = _resolved()
    now = _now()
    snapshot_session.add_all(
        [
            User(
                id=str(uuid4()),
                nickname="user",
                role="user",
                status="active",
                industries=[],
                created_at=now,
                updated_at=now,
            ),
            Tenant(
                id=tenant_id,
                slug="partial-registry",
                name="Partial Registry",
                status="active",
                is_internal=False,
                runtime_backend="pi",
                license_status="active",
                created_at=now,
                updated_at=now,
            ),
            SkillRevision(
                id=resolved.id,
                tenant_id=None,
                skill_name=resolved.skill_name,
                revision=resolved.revision,
                content=resolved.content,
                content_digest=resolved.content_digest,
                description=resolved.description,
                required_tools=[],
                artifact_contract=resolved.artifact_contract,
                created_by=None,
                created_at=now,
                change_note="test",
            ),
            SkillActivation(
                id=str(uuid4()),
                environment="production",
                tenant_id=None,
                skill_name=resolved.skill_name,
                active_revision_id=resolved.id,
                previous_revision_id=None,
                rollout_percent=100,
                updated_by=None,
                updated_at=now,
            ),
        ]
    )
    await snapshot_session.commit()

    base = build_marketing_run_capability()
    capability = await SkillSnapshotService.resolve_for_new_run(
        snapshot_session,
        tenant_id=tenant_id,
        base_capability=base,
    )

    assert {item.name for item in capability.skills} == {
        item.name for item in base.skills
    } | {resolved.skill_name}
