import os
from collections.abc import AsyncIterator, Callable, Coroutine
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession


os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("AUTH_MODE", "mock")
os.environ.setdefault("MYSQL_HOST", "127.0.0.1")
os.environ.setdefault("MYSQL_PORT", "3306")
os.environ.setdefault("MYSQL_DATABASE", "kol_insight_test")
os.environ.setdefault("MYSQL_USER", "kol_test")
os.environ.setdefault("MYSQL_PASSWORD", "test-only-password")
os.environ.setdefault("JWT_SECRET", "test-only-jwt-secret-at-least-32-characters")

from app.db.session import engine  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.identity.models import User  # noqa: E402
from app.main import create_app  # noqa: E402


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
