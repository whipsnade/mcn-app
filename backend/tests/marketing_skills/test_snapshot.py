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
    SkillManifestEntry,
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
        require_database_entries=False,
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
        require_database_entries=False,
    )

    assert {item.name for item in capability.skills} == {
        item.name for item in base.skills
    } | {resolved.skill_name}


# ---------------------------------------------------------------------------
# post-brand manifest contract（Task 2）：v1/v2 golden vectors 与 v2 冻结
# ---------------------------------------------------------------------------

def _load_digest_vectors() -> dict:
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parent / "skill_manifest_digest_vectors.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_post_brand_manifest_v1_vector_matches_golden() -> None:
    from app.marketing_skills.snapshot import _manifest_digest

    vectors = _load_digest_vectors()["v1"]
    entries = tuple(SkillManifestEntry.model_validate(item) for item in vectors["entries"])
    assert (
        _manifest_digest(entries, vectors["source_scope"], None)
        == vectors["expected_digest"]
    )


def test_post_brand_manifest_v2_vector_matches_golden() -> None:
    from app.marketing_skills.snapshot import _manifest_digest

    vectors = _load_digest_vectors()["v2"]
    entries = tuple(SkillManifestEntry.model_validate(item) for item in vectors["entries"])
    assert (
        _manifest_digest(entries, vectors["source_scope"], "skill_manifest_v2")
        == vectors["expected_digest"]
    )
    assert vectors["expected_digest"] != _load_digest_vectors()["v1"]["expected_digest"]


def test_post_brand_manifest_v2_freezes_identity_and_contract() -> None:
    from app.marketing_skills.repository import ResolvedSkillRevision
    from app.marketing_skills.snapshot import SkillManifest

    revisions = (
        ResolvedSkillRevision(
            id="aaaa1111-1111-4111-8111-111111111111",
            tenant_id=None,
            skill_name="freeze-skill",
            revision=5,
            content="freeze body",
            content_digest=canonical_skill_digest("freeze body"),
            description="freeze",
            required_tools=("load_marketing_skill",),
            artifact_contract="marketing_root_v1",
            model_input_contract_version="direct_model_input_v1",
        ),
    )
    manifest = SkillManifest.from_revisions(revisions)
    assert manifest.schema_version == "skill_manifest_v2"
    entry = manifest.entries[0]
    assert entry.revision_id == "aaaa1111-1111-4111-8111-111111111111"
    assert entry.scope_key == "__global__"
    assert entry.model_input_contract_version == "direct_model_input_v1"


def test_post_brand_manifest_v2_rejects_contract_conflict() -> None:
    import pytest as _pytest

    from app.marketing_skills.snapshot import SkillManifest

    base = {
        "revision": 1,
        "content_digest": canonical_skill_digest("conflict body"),
        "description": "x",
        "required_tools": ["load_marketing_skill"],
        "content": "conflict body",
        "revision_id": "bbbb2222-2222-4222-8222-222222222222",
        "scope_key": "__global__",
    }
    payload = {
        "entries": [
            {**base, "name": "skill-a", "artifact_contract": "brand_report_v3",
             "model_input_contract_version": "direct_model_input_v1"},
            {**base, "name": "skill-b", "artifact_contract": "brand_report_v3",
             "model_input_contract_version": "source_bound_input_v2",
             "revision_id": "cccc3333-3333-4333-8333-333333333333"},
        ],
        "manifest_digest": "0" * 64,
        "source_scope": "database_activation",
        "schema_version": "skill_manifest_v2",
    }
    with _pytest.raises(Exception):
        SkillManifest.model_validate(payload)
