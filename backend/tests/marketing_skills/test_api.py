from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.admin.models import AdminAuditLog, AdminIdempotencyRecord
from app.db.base import Base
from app.db.session import get_db
from app.identity.dependencies import require_admin
from app.identity.models import User
from app.marketing_skills.models import SkillActivation, SkillRevision
from app.marketing_skills.router import router
from app.mcp_gateway.models import McpToolCatalog
from app.tenancy.models import Tenant


VALID_CONTENT = """---
name: campaign-research
description: 活动研究
required_tools: []
---

输出活动研究。
"""


@pytest_asyncio.fixture
async def api_context():
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
        db.add(admin)
        await db.commit()
        app = FastAPI()
        app.include_router(router, prefix="/admin/skills")

        async def override_db():
            yield db

        async def override_admin():
            return admin

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[require_admin] = override_admin
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client
    await engine.dispose()


@pytest.mark.asyncio
async def test_skill_api_requires_idempotency_key_and_returns_database_diff(api_context) -> None:
    client = api_context

    missing_key = await client.post(
        "/admin/skills/campaign-research/revisions",
        json={"content": VALID_CONTENT, "change_note": "v1"},
    )
    assert missing_key.status_code == 400
    assert missing_key.json()["detail"] == "admin_idempotency_key_required"

    created = await client.post(
        "/admin/skills/campaign-research/revisions",
        headers={"Idempotency-Key": "api-create-1"},
        json={"content": VALID_CONTENT, "change_note": "v1"},
    )
    assert created.status_code == 201
    assert created.json()["revision"] == 1

    replay = await client.post(
        "/admin/skills/campaign-research/revisions",
        headers={"Idempotency-Key": "api-create-1"},
        json={"content": VALID_CONTENT, "change_note": "v1"},
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == created.json()["id"]

    second = await client.post(
        "/admin/skills/campaign-research/revisions",
        headers={"Idempotency-Key": "api-create-2"},
        json={
            "content": VALID_CONTENT.replace("活动研究", "活动研究 v2"),
            "change_note": "v2",
        },
    )
    assert second.status_code == 201

    diff = await client.get("/admin/skills/campaign-research/diff?from_revision=1&to_revision=2")
    assert diff.status_code == 200
    assert "活动研究 v2" in diff.json()["diff"]


@pytest.mark.asyncio
async def test_skill_api_validate_rejects_unknown_tool_without_model_or_datatap(api_context) -> None:
    client = api_context

    response = await client.post(
        "/admin/skills/validate",
        json={
            "expected_name": "campaign-research",
            "content": VALID_CONTENT.replace(
                "required_tools: []", "required_tools:\n  - unreviewed.tool"
            ),
        },
    )

    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert any(item["code"] == "unknown_required_tool" for item in response.json()["errors"])
