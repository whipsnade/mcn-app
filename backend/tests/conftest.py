import os
from collections.abc import AsyncIterator, Callable, Coroutine
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, insert, select, update
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import AsyncSession


os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("AUTH_MODE", "mock")
os.environ.setdefault("MYSQL_HOST", "127.0.0.1")
os.environ.setdefault("MYSQL_PORT", "3306")
os.environ.setdefault("MYSQL_DATABASE", "kol_insight_test")
os.environ.setdefault("MYSQL_USER", "kol_test")
os.environ.setdefault("MYSQL_PASSWORD", "test-only-password")
os.environ.setdefault("JWT_SECRET", "test-only-jwt-secret-at-least-32-characters")
# 测试环境确定性：enforce 开关一律由用例显式控制，不受开发 .env 影响。
os.environ.setdefault("GOAL_PLANNER_ENFORCE_ENABLED", "false")

from app.db.session import engine  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.identity.models import User  # noqa: E402
from app.main import create_app  # noqa: E402
from app.agent_runtime.models import AgentRun, AgentSession  # noqa: E402
from app.licensing.models import TenantLicense  # noqa: E402
from app.runtime_config.models import RuntimeConfigVersion  # noqa: E402
from app.runtime_config.service import LEGACY_RUNTIME_CONFIG_ID  # noqa: E402
from app.tenancy.models import Tenant, TenantMembership  # noqa: E402
from app.tenancy.service import TenantService  # noqa: E402


_LEGACY_TEST_TENANT_ID = "00000000-0000-4000-8000-000000000037"


@event.listens_for(Session, "before_flush")
def _fill_legacy_test_tenant(session: Session, _flush_context, _instances) -> None:
    """为旧单元测试直接构造的 Run/Session 提供显式测试租户。

    生产创建器不依赖这个测试钩子；它只让迁移后的 NOT NULL tenant_id 不迫使
    历史纯单元 fixture 重写业务语义。带真实 tenant_id 的对象完全不改写。
    """
    pending = [item for item in session.new if isinstance(item, (AgentSession, AgentRun))]
    needs_runtime_defaults = any(
        isinstance(item, AgentRun)
        and (
            item.runtime_backend is None
            or item.runtime_config_version_id is None
            or item.runtime_config_snapshot_json is None
            or item.queued_at is None
        )
        for item in pending
    )
    if not any(getattr(item, "tenant_id", None) is None for item in pending) and not needs_runtime_defaults:
        return
    tenant_by_user: dict[str, str] = {}
    pending_memberships = {
        item.user_id: item.tenant_id
        for item in session.new
        if isinstance(item, TenantMembership)
    }
    for user_id in {
        item.user_id for item in pending if getattr(item, "user_id", None) is not None
    }:
        if user_id in pending_memberships:
            tenant_by_user[user_id] = pending_memberships[user_id]
            continue
        with session.no_autoflush:
            existing_tenant_id = session.scalar(
                select(TenantMembership.tenant_id).where(TenantMembership.user_id == user_id)
            )
        if existing_tenant_id is not None:
            tenant_by_user[user_id] = existing_tenant_id
            continue
        known_tenant = any(
            isinstance(item, Tenant) and item.id == _LEGACY_TEST_TENANT_ID
            for item in (*session.new, *session.identity_map.values())
        )
        if not known_tenant:
            with session.no_autoflush:
                known_tenant = session.get(Tenant, _LEGACY_TEST_TENANT_ID) is not None
        if not known_tenant:
            now = datetime.now(UTC).replace(tzinfo=None)
            session.connection().execute(
                insert(Tenant).values(
                    id=_LEGACY_TEST_TENANT_ID,
                    slug="legacy-test-tenant",
                    name="测试租户",
                    status="active",
                    is_internal=True,
                    runtime_backend="current",
                    license_status="active",
                    active_license_id=None,
                    created_at=now,
                    updated_at=now,
                )
            )
        with session.no_autoflush:
            test_tenant = session.get(Tenant, _LEGACY_TEST_TENANT_ID)
        if test_tenant is not None and test_tenant.active_license_id is None:
            now = datetime.now(UTC).replace(tzinfo=None).replace(microsecond=0)
            license_id = str(uuid4())
            session.connection().execute(
                insert(TenantLicense).values(
                    id=license_id,
                    tenant_id=_LEGACY_TEST_TENANT_ID,
                    version=1,
                    valid_from=now,
                    valid_until=None,
                    features_json={
                        "kol_selection": True,
                        "brand_analysis": True,
                        "campaign_analysis": True,
                        "kol_detail": True,
                        "utility": True,
                    },
                    max_concurrent_runs=4,
                    max_user_concurrent_runs=2,
                    created_by=user_id,
                    created_at=now,
                )
            )
            session.connection().execute(
                update(Tenant)
                .where(Tenant.id == _LEGACY_TEST_TENANT_ID)
                .values(active_license_id=license_id)
            )
        session.add(
            TenantMembership(
                id=str(uuid4()),
                tenant_id=_LEGACY_TEST_TENANT_ID,
                user_id=user_id,
                role="owner",
                status="active",
                created_at=datetime.now(UTC).replace(tzinfo=None),
                updated_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )
        tenant_by_user[user_id] = _LEGACY_TEST_TENANT_ID
    for item in pending:
        if isinstance(item, AgentSession) and item.tenant_id is None:
            item.tenant_id = tenant_by_user[item.user_id]
    sessions_by_id = {
        item.id: item
        for item in (*session.new, *session.identity_map.values())
        if isinstance(item, AgentSession)
    }
    for item in pending:
        if isinstance(item, AgentRun) and item.tenant_id is None:
            parent_session = sessions_by_id.get(item.session_id)
            item.tenant_id = (
                parent_session.tenant_id
                if parent_session is not None and parent_session.tenant_id is not None
                else tenant_by_user[item.user_id]
            )

    if needs_runtime_defaults:
        with session.no_autoflush:
            legacy_config = session.get(RuntimeConfigVersion, LEGACY_RUNTIME_CONFIG_ID)
        if legacy_config is None:
            now = datetime.now(UTC).replace(tzinfo=None)
            session.connection().execute(
                insert(RuntimeConfigVersion).values(
                    id=LEGACY_RUNTIME_CONFIG_ID,
                    scope="system",
                    tenant_id=None,
                    version=1,
                    status="active",
                    runtime_backend="current",
                    runtime_contract_version="marketing_runtime_v1",
                    config_json={
                        "config_version_id": LEGACY_RUNTIME_CONFIG_ID,
                        "runtime_contract_version": "marketing_runtime_v1",
                        "runtime_backend": "current",
                        "model": {"name": "legacy-test", "masked_origin": "test"},
                        "datatap": {"service": "test", "schema_digest": "test"},
                        "capability_pack": {"runtime_contract_version": "marketing_runtime_v1"},
                        "limits": {"max_decisions": 50},
                        "billing": {"mcp_call_points": 10},
                    },
                    secret_refs_json=[],
                    created_by=None,
                    created_at=now,
                    activated_at=now,
                )
            )
        for item in pending:
            if not isinstance(item, AgentRun):
                continue
            item.runtime_backend = item.runtime_backend or "current"
            item.runtime_config_version_id = item.runtime_config_version_id or LEGACY_RUNTIME_CONFIG_ID
            item.runtime_config_snapshot_json = item.runtime_config_snapshot_json or {
                "config_version_id": LEGACY_RUNTIME_CONFIG_ID,
                "runtime_contract_version": "marketing_runtime_v1",
                "runtime_backend": "current",
                "model": {"name": "legacy-test", "masked_origin": "test"},
                "datatap": {"service": "test", "schema_digest": "test"},
                "capability_pack": {"runtime_contract_version": "marketing_runtime_v1"},
                "limits": {"max_decisions": 50},
                "billing": {"mcp_call_points": 10},
            }
            item.queued_at = item.queued_at or item.created_at or datetime.now(UTC).replace(tzinfo=None)


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    async with engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()


@pytest_asyncio.fixture
async def user_factory(
    db_session: AsyncSession,
) -> Callable[[], Coroutine[Any, Any, User]]:
    async def create_user() -> User:
        now = datetime.now(UTC).replace(tzinfo=None)
        user = User(
            id=str(uuid4()),
            nickname="测试用户",
            role="user",
            status="active",
            created_at=now,
            updated_at=now,
        )
        db_session.add(user)
        await db_session.flush()
        await TenantService(db_session).provision_personal_tenant(
            user.id, name=user.nickname, now=now
        )
        return user

    return create_user


@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    app = create_app()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client


@pytest_asyncio.fixture
async def auth_client_factory(db_session: AsyncSession):
    clients: list[AsyncClient] = []

    async def create_client(phone: str) -> AsyncClient:
        app = create_app()

        async def override_get_db() -> AsyncIterator[AsyncSession]:
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        test_client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        login = await test_client.post(
            "/api/v1/auth/mock/sms/login",
            json={"phone": phone, "code": "000000"},
        )
        test_client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
        clients.append(test_client)
        return test_client

    yield create_client
    for test_client in clients:
        await test_client.aclose()
