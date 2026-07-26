import asyncio
import json
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.main import create_app
from app.thinking.contracts import ThinkingEvent


@pytest.mark.asyncio
async def test_session_thinking_events_require_session_owner(auth_client_factory) -> None:
    owner = await auth_client_factory("13500000101")
    stranger = await auth_client_factory("13500000102")
    session_id = (await owner.post("/api/v1/sessions", json={})).json()["id"]

    response = await stranger.get(
        f"/api/v1/sessions/{session_id}/events",
        headers={"Accept": "text/event-stream"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "session_not_found"


class ClosingThinkingService:
    def __init__(self) -> None:
        self.unsubscribed = False

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
                },
            )
        )
        queue.put_nowait(None)
        return queue

    async def unsubscribe(
        self, session_id: str, queue: asyncio.Queue[ThinkingEvent | None]
    ) -> None:
        self.unsubscribed = True


@pytest.mark.asyncio
async def test_session_thinking_events_emit_sse_snapshot_keepalive_and_cleanup(
    db_session: AsyncSession,
) -> None:
    try:
        from app.thinking.router import get_session_thinking_service
    except ModuleNotFoundError:
        pytest.fail("会话思考 SSE 路由尚未实现")

    app = create_app()
    service = ClosingThinkingService()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

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
        session_id = (await client.post("/api/v1/sessions", json={})).json()["id"]

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
