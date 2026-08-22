"""post-brand 差距冻结：成功 Run Skill Snapshot 的固化导出（Task 1）。

只读校验历史 Run 的 skill_manifest 与数据库 SkillRevision：候选扫描不输出
正文；导出必须由显式 source map（revision_id + scope_key）逐 entry 选定，
不得用当前 Activation 或 created_at 猜历史 scope。

单测使用合成 run 前缀/skill 名/随机 Revision ID，避免与 kol_insight_test 中
真实 a04213cf Run 及其 Revision（4eb2581a-…）碰撞；真实锚点仅用于 CLI 生成
提交 fixture 的 Step 4。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.agent_runtime.models import AgentRun, AgentSession
from app.marketing_skills.models import SkillRevision
from app.marketing_skills.promotion import (
    SkillRevisionSource,
    list_post_brand_skill_source_candidates,
    load_post_brand_skill_snapshot,
)
from app.marketing_skills.snapshot import SkillManifestEntry, _manifest_digest
from app.marketing_skills.validation import canonical_skill_digest
from app.tenancy.models import TenantMembership

TEST_PREFIX = "pbtstrun"
SOCIAL_NAME = "social-promo-test"
BRAND_NAME = "brand-promo-test"

SOCIAL_CONTENT = f"""---
name: {SOCIAL_NAME}
description: 总则
required_tools:
  - load_marketing_skill
  - request_clarification
  - publish_artifacts
artifact_contract: marketing_root_v1
---

# 社媒营销分析总则（测试正文 rev3）
"""

BRAND_CONTENT = f"""---
name: {BRAND_NAME}
description: 品牌报告
required_tools:
  - build_artifact_draft
  - publish_artifacts
artifact_contract: brand_report_v3
---

# 品牌社媒研究报告（测试正文）
"""


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _revision_rows() -> list[SkillRevision]:
    now = _now()
    return [
        SkillRevision(
            id=str(uuid4()),
            tenant_id=None,
            skill_name=SOCIAL_NAME,
            revision=3,
            content=SOCIAL_CONTENT,
            content_digest=canonical_skill_digest(SOCIAL_CONTENT),
            description="总则",
            required_tools=["load_marketing_skill", "request_clarification", "publish_artifacts"],
            artifact_contract="marketing_root_v1",
            created_by=None,
            created_at=now,
            change_note=None,
            scope_key="__global__",
        ),
        SkillRevision(
            id=str(uuid4()),
            tenant_id=None,
            skill_name=BRAND_NAME,
            revision=2,
            content=BRAND_CONTENT,
            content_digest=canonical_skill_digest(BRAND_CONTENT),
            description="品牌报告",
            required_tools=["build_artifact_draft", "publish_artifacts"],
            artifact_contract="brand_report_v3",
            created_by=None,
            created_at=now,
            change_note=None,
            scope_key="__global__",
        ),
    ]


def _manifest_entry(row: SkillRevision) -> dict:
    return {
        "name": row.skill_name,
        "revision": row.revision,
        "content_digest": row.content_digest,
        "description": row.description,
        "required_tools": row.required_tools,
        "artifact_contract": row.artifact_contract,
        "content": row.content,
    }


async def _seed_run(
    db_session, user_factory, *, run_id: str, rows: list[SkillRevision]
) -> str:
    user = await user_factory()
    membership = await db_session.scalar(
        select(TenantMembership).where(TenantMembership.user_id == user.id)
    )
    assert membership is not None
    now = _now()
    session = AgentSession(
        id=str(uuid4()), user_id=user.id, tenant_id=membership.tenant_id, title="post-brand seed",
        status="active", created_at=now, updated_at=now,
    )
    db_session.add(session)
    await db_session.flush()
    entries = [_manifest_entry(row) for row in rows]
    ordered = sorted(entries, key=lambda item: item["name"])
    validated = tuple(SkillManifestEntry.model_validate(item) for item in ordered)
    digest = _manifest_digest(validated, "database_activation")
    db_session.add(AgentRun(
        id=run_id,
        session_id=session.id,
        user_id=user.id,
        tenant_id=membership.tenant_id,
        runtime_backend="pi",
        runtime_config_version_id=None,
        runtime_config_snapshot_json={
            "skill_manifest": {
                "entries": ordered,
                "manifest_digest": digest,
                "source_scope": "database_activation",
            },
        },
        queued_at=None,
        profile_name="pi_production",
        profile_version="v1",
        model="deepseek-v4-pro",
        status="completed",
        decision_count=1,
        review_count=0,
        revision_count=0,
        created_at=now,
        started_at=now,
        run_kind="user",
    ))
    await db_session.flush()
    return membership.tenant_id


def _full_map(rows: list[SkillRevision]) -> dict[str, SkillRevisionSource]:
    return {
        row.skill_name: SkillRevisionSource(revision_id=row.id, scope_key="__global__")
        for row in rows
    }


@pytest.mark.asyncio
async def test_post_brand_snapshot_export_freezes_revisions_by_explicit_map(
    db_session, user_factory
) -> None:
    rows = _revision_rows()
    db_session.add_all(rows)
    social = next(row for row in rows if row.skill_name == SOCIAL_NAME)
    full_run_id = f"{TEST_PREFIX}{uuid4().hex[:8]}"
    await _seed_run(db_session, user_factory, run_id=full_run_id, rows=rows)

    snapshot = await load_post_brand_skill_snapshot(
        db_session, run_prefix=TEST_PREFIX, source_map=_full_map(rows)
    )

    assert snapshot.run_id == full_run_id
    assert snapshot.entries[SOCIAL_NAME].revision == 3
    assert snapshot.entries[SOCIAL_NAME].content_digest == canonical_skill_digest(SOCIAL_CONTENT)
    assert snapshot.entries[SOCIAL_NAME].revision_id == social.id
    assert snapshot.entries[SOCIAL_NAME].scope_key == "__global__"
    assert snapshot.entries[SOCIAL_NAME].required_tools == (
        "load_marketing_skill",
        "request_clarification",
        "publish_artifacts",
    )
    dumped = snapshot.model_dump(mode="json")
    assert "runtime_config_snapshot_json" not in dumped
    assert dumped["entries"][SOCIAL_NAME]["content"] == SOCIAL_CONTENT
    assert set(dumped["entries"]) == {SOCIAL_NAME, BRAND_NAME}


@pytest.mark.asyncio
async def test_post_brand_prefix_must_match_exactly_one_run(db_session, user_factory) -> None:
    rows = _revision_rows()
    db_session.add_all(rows)
    await _seed_run(db_session, user_factory, run_id=f"{TEST_PREFIX}{uuid4().hex[:8]}", rows=rows)
    await _seed_run(db_session, user_factory, run_id=f"{TEST_PREFIX}{uuid4().hex[:8]}", rows=rows)

    with pytest.raises(ValueError, match="skill_seed_source_ambiguous"):
        await load_post_brand_skill_snapshot(
            db_session, run_prefix=TEST_PREFIX, source_map=_full_map(rows)
        )
    with pytest.raises(ValueError, match="skill_seed_source_ambiguous"):
        await list_post_brand_skill_source_candidates(db_session, run_prefix=TEST_PREFIX)


@pytest.mark.asyncio
async def test_post_brand_missing_run_raises_source_missing(db_session) -> None:
    with pytest.raises(ValueError, match="skill_seed_source_missing"):
        await load_post_brand_skill_snapshot(
            db_session, run_prefix="pbnoexist", source_map={}
        )


@pytest.mark.asyncio
async def test_post_brand_map_keys_must_match_manifest_entries(db_session, user_factory) -> None:
    rows = _revision_rows()
    db_session.add_all(rows)
    await _seed_run(db_session, user_factory, run_id=f"{TEST_PREFIX}{uuid4().hex[:8]}", rows=rows)
    social = next(row for row in rows if row.skill_name == SOCIAL_NAME)
    brand = next(row for row in rows if row.skill_name == BRAND_NAME)

    # map 少 key（缺 brand）
    with pytest.raises(ValueError, match="skill_seed_revision_mismatch"):
        await load_post_brand_skill_snapshot(
            db_session,
            run_prefix=TEST_PREFIX,
            source_map={SOCIAL_NAME: SkillRevisionSource(revision_id=social.id, scope_key="__global__")},
        )

    # map 多 key（manifest 没有的 skill）
    with pytest.raises(ValueError, match="skill_seed_revision_mismatch"):
        await load_post_brand_skill_snapshot(
            db_session,
            run_prefix=TEST_PREFIX,
            source_map={
                **_full_map(rows),
                "analysis-report": SkillRevisionSource(revision_id=brand.id, scope_key="__global__"),
            },
        )


@pytest.mark.asyncio
async def test_post_brand_revision_id_outside_declared_scope_is_rejected(
    db_session, user_factory
) -> None:
    rows = _revision_rows()
    db_session.add_all(rows)
    run_tenant = await _seed_run(
        db_session, user_factory, run_id=f"{TEST_PREFIX}{uuid4().hex[:8]}", rows=rows
    )

    # 造一个 Run tenant scope 的 brand revision，但 map 声明它为 __global__
    tenant_row = SkillRevision(
        id=str(uuid4()),
        tenant_id=run_tenant,
        skill_name=BRAND_NAME,
        revision=2,
        content=BRAND_CONTENT,
        content_digest=canonical_skill_digest(BRAND_CONTENT),
        description="品牌报告",
        required_tools=["build_artifact_draft", "publish_artifacts"],
        artifact_contract="brand_report_v3",
        created_by=None,
        created_at=_now(),
        change_note=None,
        scope_key=run_tenant,
    )
    db_session.add(tenant_row)
    await db_session.flush()

    with pytest.raises(ValueError, match="skill_seed_revision_mismatch"):
        await load_post_brand_skill_snapshot(
            db_session,
            run_prefix=TEST_PREFIX,
            source_map={
                SOCIAL_NAME: next(
                    SkillRevisionSource(revision_id=row.id, scope_key="__global__")
                    for row in rows
                    if row.skill_name == SOCIAL_NAME
                ),
                BRAND_NAME: SkillRevisionSource(revision_id=tenant_row.id, scope_key="__global__"),
            },
        )


@pytest.mark.asyncio
async def test_post_brand_twin_scope_candidates_require_explicit_map(
    db_session, user_factory
) -> None:
    rows = _revision_rows()
    db_session.add_all(rows)
    run_tenant = await _seed_run(
        db_session, user_factory, run_id=f"{TEST_PREFIX}{uuid4().hex[:8]}", rows=rows
    )
    brand = next(row for row in rows if row.skill_name == BRAND_NAME)

    # global 与 Run tenant 各有一份完全相同的 brand revision（name/rev/digest/content）
    twin = SkillRevision(
        id=str(uuid4()),
        tenant_id=run_tenant,
        skill_name=BRAND_NAME,
        revision=2,
        content=BRAND_CONTENT,
        content_digest=canonical_skill_digest(BRAND_CONTENT),
        description="品牌报告",
        required_tools=["build_artifact_draft", "publish_artifacts"],
        artifact_contract="brand_report_v3",
        created_by=None,
        created_at=_now(),
        change_note=None,
        scope_key=run_tenant,
    )
    db_session.add(twin)
    await db_session.flush()

    candidates = await list_post_brand_skill_source_candidates(db_session, run_prefix=TEST_PREFIX)
    brand_entry = next(item for item in candidates.entries if item.name == BRAND_NAME)
    assert len(brand_entry.candidates) == 2
    assert {item.scope_key for item in brand_entry.candidates} == {"__global__", run_tenant}

    # 显式选定 global 后导出成功；选定 tenant twin 也可（显式即明确）
    snapshot = await load_post_brand_skill_snapshot(
        db_session,
        run_prefix=TEST_PREFIX,
        source_map={
            **_full_map(rows),
            BRAND_NAME: SkillRevisionSource(revision_id=brand.id, scope_key="__global__"),
        },
    )
    assert snapshot.entries[BRAND_NAME].revision_id == brand.id
    snapshot_twin = await load_post_brand_skill_snapshot(
        db_session,
        run_prefix=TEST_PREFIX,
        source_map={
            **_full_map(rows),
            BRAND_NAME: SkillRevisionSource(revision_id=twin.id, scope_key=run_tenant),
        },
    )
    assert snapshot_twin.entries[BRAND_NAME].revision_id == twin.id


@pytest.mark.asyncio
async def test_post_brand_missing_revision_raises_source_missing(db_session, user_factory) -> None:
    rows = _revision_rows()
    db_session.add_all(rows)
    await _seed_run(db_session, user_factory, run_id=f"{TEST_PREFIX}{uuid4().hex[:8]}", rows=rows)

    with pytest.raises(ValueError, match="skill_seed_source_missing"):
        await load_post_brand_skill_snapshot(
            db_session,
            run_prefix=TEST_PREFIX,
            source_map={
                **_full_map(rows),
                BRAND_NAME: SkillRevisionSource(revision_id=str(uuid4()), scope_key="__global__"),
            },
        )


@pytest.mark.asyncio
async def test_post_brand_digest_mismatch_between_manifest_and_db(db_session, user_factory) -> None:
    rows = _revision_rows()
    db_session.add_all(rows)

    # manifest 声称 brand 的 digest 属于另一份正文
    tampered_content = "different content"
    tampered_digest = canonical_skill_digest(tampered_content)
    user = await user_factory()
    membership = await db_session.scalar(
        select(TenantMembership).where(TenantMembership.user_id == user.id)
    )
    now = _now()
    session = AgentSession(
        id=str(uuid4()), user_id=user.id, tenant_id=membership.tenant_id, title="tampered",
        status="active", created_at=now, updated_at=now,
    )
    db_session.add(session)
    await db_session.flush()
    entries = [_manifest_entry(row) for row in rows]
    for item in entries:
        if item["name"] == BRAND_NAME:
            item["content"] = tampered_content
            item["content_digest"] = tampered_digest
    ordered = sorted(entries, key=lambda item: item["name"])
    validated = tuple(SkillManifestEntry.model_validate(item) for item in ordered)
    digest = _manifest_digest(validated, "database_activation")
    db_session.add(AgentRun(
        id=f"{TEST_PREFIX}{uuid4().hex[:8]}",
        session_id=session.id,
        user_id=user.id,
        tenant_id=membership.tenant_id,
        runtime_backend="pi",
        runtime_config_version_id=None,
        runtime_config_snapshot_json={
            "skill_manifest": {"entries": ordered, "manifest_digest": digest, "source_scope": "database_activation"},
        },
        queued_at=None, profile_name="pi_production", profile_version="v1", model="m",
        status="completed", decision_count=1, review_count=0, revision_count=0,
        created_at=now, started_at=now, run_kind="user",
    ))
    await db_session.flush()

    with pytest.raises(ValueError, match="skill_seed_digest_mismatch"):
        await load_post_brand_skill_snapshot(
            db_session, run_prefix=TEST_PREFIX, source_map=_full_map(rows)
        )


@pytest.mark.asyncio
async def test_post_brand_secret_pattern_fails_closed(db_session, user_factory) -> None:
    secret_content = (
        f"---\nname: {SOCIAL_NAME}\ndescription: 总则\n"
        "required_tools:\n  - load_marketing_skill\nartifact_contract: marketing_root_v1\n---\n\n"
        "# 正文\n\nBearer eyJhbGciOiJIUzI1NiJ9.secret-token-value\n"
    )
    row = SkillRevision(
        id=str(uuid4()),
        tenant_id=None,
        skill_name=SOCIAL_NAME,
        revision=3,
        content=secret_content,
        content_digest=canonical_skill_digest(secret_content),
        description="总则",
        required_tools=["load_marketing_skill"],
        artifact_contract="marketing_root_v1",
        created_by=None,
        created_at=_now(),
        change_note=None,
        scope_key="__global__",
    )
    db_session.add(row)
    await _seed_run(
        db_session, user_factory, run_id=f"{TEST_PREFIX}{uuid4().hex[:8]}", rows=[row]
    )

    with pytest.raises(ValueError, match="skill_seed_secret_detected"):
        await load_post_brand_skill_snapshot(
            db_session,
            run_prefix=TEST_PREFIX,
            source_map={SOCIAL_NAME: SkillRevisionSource(revision_id=row.id, scope_key="__global__")},
        )
