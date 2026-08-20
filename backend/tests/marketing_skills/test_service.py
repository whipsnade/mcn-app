from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.admin.models import AdminAuditLog, AdminIdempotencyRecord
from app.db.base import Base
from app.identity.models import User
from app.marketing_skills.models import SkillActivation, SkillRevision
from app.marketing_skills.schemas import (
    SkillActivationRequest,
    SkillRevisionCreate,
    SkillRollbackRequest,
    SkillValidationRequest,
)
from app.marketing_skills.service import SkillAdminError, SkillAdminService
from app.mcp_gateway.models import McpToolCatalog
from app.tenancy.models import Tenant


VALID_CONTENT = """---
name: campaign-research
description: 活动研究
required_tools: []
---

根据真实数据输出活动研究。
"""


@pytest_asyncio.fixture
async def admin_context():
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
                    AdminAuditLog.__table__,
                    AdminIdempotencyRecord.__table__,
                    McpToolCatalog.__table__,
                ],
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as db:
        now = datetime.now(UTC).replace(tzinfo=None)
        admin = User(
            id=str(uuid4()),
            nickname="管理员",
            role="admin",
            status="active",
            industries=[],
            created_at=now,
            updated_at=now,
        )
        tenant = Tenant(
            id=str(uuid4()),
            slug="brand-tenant",
            name="品牌租户",
            status="active",
            is_internal=False,
            runtime_backend="pi",
            license_status="active",
            created_at=now,
            updated_at=now,
        )
        db.add_all([admin, tenant])
        await db.commit()
        yield db, admin, tenant
    await engine.dispose()


@pytest.mark.asyncio
async def test_create_revision_is_validated_audited_and_idempotent(admin_context) -> None:
    db, admin, _tenant = admin_context
    service = SkillAdminService(db, approved_tools=set())
    payload = SkillRevisionCreate(content=VALID_CONTENT, change_note="初始版本")

    first = await service.create_revision(
        admin,
        "campaign-research",
        payload,
        idempotency_key="skill-create-1",
    )
    replay = await service.create_revision(
        admin,
        "campaign-research",
        payload,
        idempotency_key="skill-create-1",
    )
    await db.commit()

    assert first.id == replay.id
    assert first.revision == 1
    assert first.content_digest
    assert await db.scalar(
        select(AdminAuditLog).where(AdminAuditLog.action == "skill.revision_create")
    )
    assert await db.scalar(
        select(AdminIdempotencyRecord).where(
            AdminIdempotencyRecord.idempotency_key == "skill-create-1"
        )
    )


@pytest.mark.asyncio
async def test_create_revision_same_key_with_different_payload_is_conflict(admin_context) -> None:
    db, admin, _tenant = admin_context
    service = SkillAdminService(db, approved_tools=set())
    first_payload = SkillRevisionCreate(content=VALID_CONTENT, change_note="v1")
    second_payload = SkillRevisionCreate(
        content=VALID_CONTENT + "\n追加说明\n", change_note="v2"
    )

    await service.create_revision(
        admin,
        "campaign-research",
        first_payload,
        idempotency_key="skill-create-conflict",
    )
    with pytest.raises(SkillAdminError, match="admin_idempotency_conflict"):
        await service.create_revision(
            admin,
            "campaign-research",
            second_payload,
            idempotency_key="skill-create-conflict",
        )


@pytest.mark.asyncio
async def test_global_skill_revision_scope_is_database_unique(admin_context) -> None:
    db, _admin, _tenant = admin_context
    now = datetime.now(UTC).replace(tzinfo=None)
    rows = [
        SkillRevision(
            id=str(uuid4()),
            tenant_id=None,
            skill_name="campaign-research",
            revision=1,
            content=f"global-{index}",
            content_digest=f"{index:064d}",
            description="global",
            required_tools=[],
            created_by=None,
            created_at=now,
            change_note=None,
        )
        for index in range(2)
    ]
    db.add_all(rows)

    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()


@pytest.mark.asyncio
async def test_global_skill_activation_scope_is_database_unique(admin_context) -> None:
    db, _admin, _tenant = admin_context
    now = datetime.now(UTC).replace(tzinfo=None)
    revision = SkillRevision(
        id=str(uuid4()),
        tenant_id=None,
        skill_name="campaign-research",
        revision=1,
        content="global",
        content_digest="0" * 64,
        description="global",
        required_tools=[],
        created_by=None,
        created_at=now,
        change_note=None,
    )
    db.add(revision)
    await db.flush()
    db.add_all([
        SkillActivation(
            id=str(uuid4()),
            environment="production",
            tenant_id=None,
            skill_name="campaign-research",
            active_revision_id=revision.id,
            previous_revision_id=None,
            rollout_percent=100,
            updated_by=None,
            updated_at=now,
        ),
        SkillActivation(
            id=str(uuid4()),
            environment="production",
            tenant_id=None,
            skill_name="campaign-research",
            active_revision_id=revision.id,
            previous_revision_id=None,
            rollout_percent=100,
            updated_by=None,
            updated_at=now,
        ),
    ])

    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()


@pytest.mark.asyncio
async def test_validate_uses_approved_tool_set_and_diff_reads_database_content(admin_context) -> None:
    db, admin, _tenant = admin_context
    service = SkillAdminService(db, approved_tools={"datatap.approved"})
    invalid = await service.validate(
        SkillValidationRequest(
            expected_name="campaign-research",
            content=VALID_CONTENT.replace(
                "required_tools: []", "required_tools:\n  - datatap.unknown"
            ),
        )
    )
    assert not invalid.valid
    assert any(error.code == "unknown_required_tool" for error in invalid.errors)

    first = await service.create_revision(
        admin,
        "campaign-research",
        SkillRevisionCreate(content=VALID_CONTENT, change_note="v1"),
        idempotency_key="skill-create-diff-1",
    )
    second_content = VALID_CONTENT.replace("活动研究", "活动研究 v2")
    second = await service.create_revision(
        admin,
        "campaign-research",
        SkillRevisionCreate(content=second_content, change_note="v2"),
        idempotency_key="skill-create-diff-2",
    )
    diff = await service.diff(
        "campaign-research", from_revision=first.revision, to_revision=second.revision
    )

    assert "活动研究 v2" in diff.diff
    assert "活动研究\n" in diff.diff


@pytest.mark.asyncio
async def test_diff_uses_tenant_revision_context(admin_context) -> None:
    db, admin, tenant = admin_context
    service = SkillAdminService(db, approved_tools=set())
    global_v1 = await service.create_revision(
        admin,
        "campaign-research",
        SkillRevisionCreate(content=VALID_CONTENT, change_note="global v1"),
        idempotency_key="skill-diff-global-1",
    )
    global_v2 = await service.create_revision(
        admin,
        "campaign-research",
        SkillRevisionCreate(
            content=VALID_CONTENT.replace("活动研究", "全局 v2"),
            change_note="global v2",
        ),
        idempotency_key="skill-diff-global-2",
    )
    tenant_v1 = await service.create_revision(
        admin,
        "campaign-research",
        SkillRevisionCreate(
            content=VALID_CONTENT.replace("活动研究", "租户 v1"),
            tenant_id=tenant.id,
            change_note="tenant v1",
        ),
        idempotency_key="skill-diff-tenant-1",
    )
    tenant_v2 = await service.create_revision(
        admin,
        "campaign-research",
        SkillRevisionCreate(
            content=VALID_CONTENT.replace("活动研究", "租户 v2"),
            tenant_id=tenant.id,
            change_note="tenant v2",
        ),
        idempotency_key="skill-diff-tenant-2",
    )

    diff = await service.diff(
        "campaign-research",
        from_revision=tenant_v1.revision,
        to_revision=tenant_v2.revision,
        tenant_id=tenant.id,
    )

    assert "租户 v2" in diff.diff
    assert "全局 v2" not in diff.diff
    assert global_v1.revision == tenant_v1.revision
    assert global_v2.revision == tenant_v2.revision


@pytest.mark.asyncio
async def test_activate_and_rollback_preserve_previous_pointer(admin_context) -> None:
    db, admin, tenant = admin_context
    service = SkillAdminService(db, approved_tools=set())
    first = await service.create_revision(
        admin,
        "campaign-research",
        SkillRevisionCreate(content=VALID_CONTENT, change_note="v1"),
        idempotency_key="skill-create-activate-1",
    )
    second = await service.create_revision(
        admin,
        "campaign-research",
        SkillRevisionCreate(
            content=VALID_CONTENT.replace("活动研究", "活动研究 v2"), change_note="v2"
        ),
        idempotency_key="skill-create-activate-2",
    )
    activated = await service.activate(
        admin,
        "campaign-research",
        SkillActivationRequest(
            revision=first.revision,
            tenant_id=tenant.id,
            rollout_percent=100,
        ),
        idempotency_key="skill-activate-1",
    )
    await service.activate(
        admin,
        "campaign-research",
        SkillActivationRequest(
            revision=second.revision,
            tenant_id=tenant.id,
            rollout_percent=20,
        ),
        idempotency_key="skill-activate-2",
    )
    rolled_back = await service.rollback(
        admin,
        "campaign-research",
        SkillRollbackRequest(tenant_id=tenant.id),
        idempotency_key="skill-rollback-1",
    )
    await db.commit()

    assert activated.active_revision == first.revision
    assert rolled_back.active_revision == first.revision
    assert rolled_back.previous_revision == second.revision

    with pytest.raises(SkillAdminError, match="tenant_not_found"):
        await service.activate(
            admin,
            "campaign-research",
            SkillActivationRequest(revision=1, tenant_id=str(uuid4())),
            idempotency_key="skill-activate-missing-tenant",
        )
