"""post-brand bootstrap bundle 与 production fail-closed（Task 2）。"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from app.marketing_capability_pack.runtime import MarketingRunCapability, MarketingSkillSnapshot
from app.marketing_skills.bootstrap import (
    PostBrandBootstrapBundle,
    SkillBootstrapError,
    load_post_brand_bootstrap,
    validate_bootstrap_digest,
)
from app.marketing_skills.snapshot import SkillSnapshotError, SkillSnapshotService

FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent / "marketing_skills" / "post_brand_success_skill_manifest.json"
)


def _fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_post_brand_bundle_matches_success_fixture() -> None:
    bundle = load_post_brand_bootstrap()
    fixture = _fixture()

    assert bundle.source_manifest_digest == fixture["manifest_digest"]
    assert bundle.source_run_id == fixture["run_id"]
    revisions = bundle.revisions_by_skill["social-marketing-analyst"]
    assert revisions[0].revision == 3
    assert revisions[1].revision == 4
    assert "单轮调用" not in revisions[1].content
    assert "停止无效探测" in revisions[1].content
    assert revisions[0].default_activation is True
    assert revisions[0].candidate_activation is False
    assert revisions[1].default_activation is False
    assert revisions[1].candidate_activation is True
    # Revision 3 正文逐字取自成功 fixture。
    assert revisions[0].content == fixture["entries"]["social-marketing-analyst"]["content"]
    assert revisions[0].content_digest == fixture["entries"]["social-marketing-analyst"]["content_digest"]
    assert revisions[1].model_input_contract_version == "direct_model_input_v1"


def test_post_brand_bundle_validates_digests() -> None:
    bundle = load_post_brand_bootstrap()
    validate_bootstrap_digest(bundle)


def test_post_brand_bundle_tamper_fails() -> None:
    bundle = load_post_brand_bootstrap()
    tampered = PostBrandBootstrapBundle(
        name=bundle.name,
        source_run_id=bundle.source_run_id,
        source_manifest_digest=bundle.source_manifest_digest,
        revisions=bundle.revisions,
        bundle_digest="0" * 64,
    )
    with pytest.raises(SkillBootstrapError, match="skill_seed_digest_conflict"):
        validate_bootstrap_digest(tampered)


def _capability_without_db(name: str) -> MarketingRunCapability:
    import hashlib

    from app.marketing_skills.validation import canonical_skill_digest

    body = "legacy body"
    policy = "policy"
    return MarketingRunCapability(
        pack_name="marketing",
        pack_version="test",
        manifest_digest="0" * 64,
        runtime_contract_version="marketing_runtime_v1",
        root_policy=policy,
        root_policy_digest=hashlib.sha256(policy.encode("utf-8")).hexdigest(),
        skills=(
            MarketingSkillSnapshot(
                name=name,
                version="1.0.0",
                revision=None,
                digest=canonical_skill_digest(body),
                content=body,
                required_tools=("load_marketing_skill",),
                artifact_contract=None,
            ),
        ),
        artifact_contracts=(),
        builder_versions={},
        exporter_versions={},
    )


@pytest.mark.asyncio
async def test_post_brand_production_requires_database_entries(db_session, monkeypatch) -> None:
    async def empty_resolve(*args, **kwargs):
        return ()

    monkeypatch.setattr(
        "app.marketing_skills.snapshot.resolve_active_revisions", empty_resolve
    )
    with pytest.raises(SkillSnapshotError, match="skill_activation_incomplete"):
        await SkillSnapshotService.resolve_for_new_run(
            db_session,
            tenant_id=f"{uuid4()}",
            base_capability=_capability_without_db("social-marketing-analyst"),
            environment="production",
            require_database_entries=True,
        )


@pytest.mark.asyncio
async def test_post_brand_legacy_pack_scope_can_opt_out(db_session, monkeypatch) -> None:
    from app.marketing_skills.repository import ResolvedSkillRevision
    from app.marketing_skills.validation import canonical_skill_digest

    body = "db resolved body"
    resolved = (
        ResolvedSkillRevision(
            id=str(uuid4()),
            tenant_id=None,
            skill_name="legacy-skill",
            revision=1,
            content=body,
            content_digest=canonical_skill_digest(body),
            description="legacy",
            required_tools=("load_marketing_skill",),
            artifact_contract=None,
            model_input_contract_version="direct_model_input_v1",
        ),
    )

    async def one_resolve(*args, **kwargs):
        return resolved

    monkeypatch.setattr(
        "app.marketing_skills.snapshot.resolve_active_revisions", one_resolve
    )
    capability = await SkillSnapshotService.resolve_for_new_run(
        db_session,
        tenant_id=f"{uuid4()}",
        base_capability=_capability_without_db("legacy-skill"),
        environment="production",
        require_database_entries=False,
    )
    assert capability.skills[0].name == "legacy-skill"
    assert capability.skills[0].content == body
    assert capability.skills[0].revision_id == resolved[0].id
