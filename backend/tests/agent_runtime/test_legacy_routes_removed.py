"""Task 24：旧模型执行路由必须 404，存活 API（身份/钱包/管理员/收藏）继续可用。

旧执行链路（Quick、Brainstorm、Goal/Task、workspace、reporting、selection、
artifacts）的 Router 已被取消注册，替换为 agent_runtime/agent_artifacts/favorites。
本测试锁定「旧路由不可达」这一契约，防止回归。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.db.session import get_db
from app.identity.models import LoginSession, User
from app.main import create_app

SESSION_ID = "00000000-0000-0000-0000-000000000000"
TASK_ID = "00000000-0000-0000-0000-000000000001"

# 旧执行链路路由：断言均 404（已取消注册）。
LEGACY_GET_ROUTES = [
    "/api/v1/quick/kol-recommendations",
    "/api/v1/quick/kol-detail",
    "/api/v1/quick/top-posts",
    "/api/v1/tasks/{task_id}/events",
    # 旧 Session thinking SSE（ChatArea 404 重连风暴的来源）：新运行时不发布该事件。
    "/api/v1/sessions/{session_id}/events",
    "/api/v1/sessions/{session_id}/kol-selection/detail",
    "/api/v1/sessions/{session_id}/kol-selection",
    "/api/v1/sessions/{session_id}/selection-sets",
    "/api/v1/sessions/{session_id}/kol-top10-trend",
    "/api/v1/sessions/{session_id}/kol-selection/export",
    "/api/v1/analysis-reports/{task_id}",
    "/api/v1/sessions/{session_id}/reports",
    "/api/v1/sessions/{session_id}/reports/{task_id}/export",
    "/api/v1/sessions/{session_id}/artifacts/summary",
]

LEGACY_POST_ROUTES = [
    "/api/v1/quick/evaluate",
    "/api/v1/sessions/{session_id}/brainstorm",
    "/api/v1/sessions/{session_id}/tasks",
    "/api/v1/tasks/{task_id}/retry",
    "/api/v1/tasks/{task_id}/followups/retry",
    "/api/v1/tasks/{task_id}/cancel",
    "/api/v1/sessions/{session_id}/kol-selection/detail/query",
    "/api/v1/sessions/{session_id}/kol-analysis",
    "/api/v1/sessions/{session_id}/analysis-retry",
]


def _path(route: str) -> str:
    return route.format(session_id=SESSION_ID, task_id=TASK_ID)


async def _admin_client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """构造管理员客户端（admin 角色），用于验证 admin API 仍可用。"""
    app = create_app()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    now = datetime.now(UTC).replace(tzinfo=None)
    admin = User(
        id=str(uuid4()),
        nickname="管理员",
        role="admin",
        status="active",
        created_at=now,
        updated_at=now,
    )
    login_session = LoginSession(
        id=str(uuid4()),
        user_id=admin.id,
        refresh_token_hash=uuid4().hex + uuid4().hex,
        expires_at=now + timedelta(days=1),
        revoked_at=None,
        created_at=now,
        last_seen_at=now,
    )
    db_session.add(admin)
    await db_session.flush()
    db_session.add(login_session)
    await db_session.flush()
    token = create_access_token(user_id=admin.id, session_id=login_session.id, role="admin")
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    client.headers["Authorization"] = f"Bearer {token}"
    try:
        yield client
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("route", LEGACY_GET_ROUTES)
async def test_legacy_get_routes_return_404(
    auth_client_factory, route: str
) -> None:
    client = await auth_client_factory("13500000001")
    response = await client.get(_path(route))
    assert response.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("route", LEGACY_POST_ROUTES)
async def test_legacy_post_routes_return_404(
    auth_client_factory, route: str
) -> None:
    client = await auth_client_factory("13500000002")
    response = await client.post(_path(route), json={})
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_identity_auth_still_available(auth_client_factory) -> None:
    """身份/认证 API 仍可用：/users/me 返回 200。"""
    client = await auth_client_factory("13500000003")
    response = await client.get("/api/v1/users/me")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_wallet_still_available(auth_client_factory) -> None:
    """钱包 API 仍可用：/wallet 返回 200。"""
    client = await auth_client_factory("13500000004")
    response = await client.get("/api/v1/wallet")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_admin_still_available(auth_client_factory, db_session) -> None:
    """管理员 API 仍可用：admin 用户访问 /admin/users 返回 200。"""
    async for client in _admin_client(db_session):
        response = await client.get("/api/v1/admin/users")
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_favorites_still_available(auth_client_factory) -> None:
    """收藏 API 仍可用：/favorites 返回 200。"""
    client = await auth_client_factory("13500000005")
    response = await client.get("/api/v1/favorites")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_new_agent_runtime_still_available(auth_client_factory) -> None:
    """新 Agent 运行时 API 仍可用：/agent/sessions 返回 200。"""
    client = await auth_client_factory("13500000006")
    response = await client.get("/api/v1/agent/sessions")
    assert response.status_code == 200


def test_legacy_thinking_sse_route_unregistered() -> None:
    """旧 thinking SSE 路由不在应用路由表中（只校验注册，404 契约由上方参数化用例锁定）。

    归属校验失败也会返回 404，单凭状态码无法区分「路由已删除」与「会话不存在」，
    因此这里直接断言路由未注册，防止未来被重新挂载。
    """
    app = create_app()
    assert "/api/v1/sessions/{session_id}/events" not in app.openapi()["paths"]
