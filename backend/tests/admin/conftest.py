from collections.abc import AsyncIterator, Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.db.session import get_db
from app.identity.models import LoginSession, User
from app.main import create_app


@pytest_asyncio.fixture
async def authed_client_factory(
    db_session: AsyncSession,
) -> AsyncIterator[Callable[..., Coroutine[Any, Any, tuple[AsyncClient, User]]]]:
    """Build clients authenticated as a freshly-created user with any role."""
    clients: list[AsyncClient] = []

    async def create(role: str = "admin", nickname: str = "管理员") -> tuple[AsyncClient, User]:
        app = create_app()

        async def override_get_db() -> AsyncIterator[AsyncSession]:
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        now = datetime.now(UTC).replace(tzinfo=None)
        user = User(
            id=str(uuid4()),
            nickname=nickname,
            role=role,
            status="active",
            created_at=now,
            updated_at=now,
        )
        login_session = LoginSession(
            id=str(uuid4()),
            user_id=user.id,
            refresh_token_hash=uuid4().hex + uuid4().hex,
            expires_at=now + timedelta(days=1),
            revoked_at=None,
            created_at=now,
            last_seen_at=now,
        )
        db_session.add(user)
        await db_session.flush()
        db_session.add(login_session)
        await db_session.flush()
        token = create_access_token(user_id=user.id, session_id=login_session.id, role=role)
        test_client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        test_client.headers["Authorization"] = f"Bearer {token}"
        clients.append(test_client)
        return test_client, user

    yield create
    for test_client in clients:
        await test_client.aclose()
