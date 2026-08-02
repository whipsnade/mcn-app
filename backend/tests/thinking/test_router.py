import asyncio
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.identity.models import AuthIdentity
from app.main import create_app
from app.thinking.contracts import ThinkingEvent
from app.workspace.models import WorkspaceSession


async def _seed_session(db_session: AsyncSession, phone: str) -> str:
    """为指定手机号用户直接播种一个会话（legacy workspace POST /sessions 已下线）。"""
    identity = await db_session.scalar(
        select(AuthIdentity).where(
            AuthIdentity.provider == "sms", AuthIdentity.provider_subject == phone
        )
    )
    assert identity is not None
    now = datetime.now(UTC).replace(tzinfo=None)
    session = WorkspaceSession(
        id=str(uuid4()),
        user_id=identity.user_id,
        title="思考会话",
        brand="",
        status="draft",
        platforms=[],
        target_audience="",
        last_accessed_at=now,
        created_at=now,
        updated_at=now,
    )
    db_session.add(session)
    await db_session.flush()
    return session.id


@pytest.mark.asyncio
async def test_session_thinking_events_require_session_owner(
    auth_client_factory, db_session
) -> None:
    await auth_client_factory("13500000101")
    stranger = await auth_client_factory("13500000102")
    session_id = await _seed_session(db_session, "13500000101")

    response = await stranger.get(
        f"/api/v1/sessions/{session_id}/events",
        headers={"Accept": "text/event-stream"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "session_not_found"


class ClosingThinkingService:
    def __init__(self, active_dependencies: Callable[[], int] | None = None) -> None:
        self.unsubscribed = False
        self._active_dependencies = active_dependencies
        self.active_dependencies_when_unsubscribed: int | None = None

    async def subscribe(self, session_id: str) -> asyncio.Queue[ThinkingEvent | None]:
        queue: asyncio.Queue[ThinkingEvent | None] = asyncio.Queue()
        queue.put_nowait(
            ThinkingEvent(
                id="event-1",
                type="thinking.snapshot",
                payload={
                    "operation_id": "op-1",
                    "turn_id": "turn-1",
                    "session_id": session_id,
                    "text": "分析品牌",
                    "sequence": 2,
                },
            )
        )
        queue.put_nowait(None)
        return queue

    async def unsubscribe(
        self, session_id: str, queue: asyncio.Queue[ThinkingEvent | None]
    ) -> None:
        self.unsubscribed = True
        if self._active_dependencies is not None:
            self.active_dependencies_when_unsubscribed = self._active_dependencies()


@pytest.mark.asyncio
async def test_session_thinking_events_emit_sse_snapshot_keepalive_and_cleanup(
    db_session: AsyncSession,
) -> None:
    try:
        from app.thinking.router import get_session_thinking_service
    except ModuleNotFoundError:
        pytest.fail("会话思考 SSE 路由尚未实现")

    app = create_app()
    dependency_lifecycle = {"active": 0}
    service = ClosingThinkingService(lambda: dependency_lifecycle["active"])

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        dependency_lifecycle["active"] += 1
        try:
            yield db_session
        finally:
            dependency_lifecycle["active"] -= 1

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_session_thinking_service] = lambda: service
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        login = await client.post(
            "/api/v1/auth/mock/sms/login",
            json={"phone": "13500000103", "code": "000000"},
        )
        client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
        session_id = await _seed_session(db_session, "13500000103")

        response = await client.get(
            f"/api/v1/sessions/{session_id}/events",
            headers={
                "Accept": "text/event-stream",
                "Last-Event-ID": "ignored-legacy-event",
            },
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.startswith(": keepalive\n\n")
    assert "id: event-1\n" in response.text
    assert "event: thinking.snapshot\n" in response.text
    data_line = next(line for line in response.text.splitlines() if line.startswith("data: "))
    assert json.loads(data_line.removeprefix("data: "))["text"] == "分析品牌"
    assert service.unsubscribed is True
    assert service.active_dependencies_when_unsubscribed == 0
