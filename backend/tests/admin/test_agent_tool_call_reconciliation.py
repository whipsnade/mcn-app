"""管理员 tool call 核对测试（设计文档 §11.1 / §16 安全与审计）。

POST /api/v1/admin/agent-tool-calls/{call_id}/reconcile：
- 需要 admin；
- decision ∈ {confirm_success, confirm_failure, keep_unknown}；
- confirm_success + 可取回 payload → 创建 Evidence 并 settle；
- confirm_success 无 payload → settle 但不得伪造 Evidence（标记 result_unavailable）；
- confirm_failure → release；
- keep_unknown → 保持 unknown + 追加核对审计行；
- 幂等键保证重放不重复扣费/结算。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.models import AdminAuditLog
from app.agent_runtime.models import (
    AgentRun,
    AgentRunAttempt,
    AgentSession,
    AgentStep,
    AgentToolCall,
    AgentToolCallReconciliation,
    EvidenceItem,
)
from app.billing.models import TenantWalletTransaction
from app.billing.service import WalletService
from app.core.security import create_access_token
from app.db.session import get_db
from app.identity.models import LoginSession, User
from app.main import create_app
from app.mcp_gateway.transport import RemoteToolResult

LOGICAL_CALL_ID = "admin-reconcile-lc-1"


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _reserve(call: AgentToolCall, user_id: str, db_session: AsyncSession) -> None:
    await WalletService(db_session).reserve(
        user_id,
        10,
        f"agent-mcp:{call.logical_call_id}:reserve",
        call.id,
        reference_type="agent_tool_call",
    )


async def _make_unknown_call(
    db_session: AsyncSession,
    user_id: str,
    *,
    upstream_request_id: str | None = "req-1",
    status: str = "unknown",
) -> AgentToolCall:
    now = _now()
    session = AgentSession(
        id=str(uuid4()), user_id=user_id, title="会话", status="active", created_at=now, updated_at=now
    )
    db_session.add(session)
    await db_session.flush()
    run = AgentRun(
        id=str(uuid4()),
        session_id=session.id,
        user_id=user_id,
        profile_name="session_analyst_v1",
        profile_version="v1",
        model="test-model",
        status="running",
    )
    db_session.add(run)
    await db_session.flush()
    attempt = AgentRunAttempt(id=str(uuid4()), run_id=run.id, attempt=1, started_at=now)
    db_session.add(attempt)
    await db_session.flush()
    step = AgentStep(
        id=str(uuid4()),
        run_id=run.id,
        attempt_id=attempt.id,
        sequence=1,
        step_type="tool_call",
        status="running",
        created_at=now,
    )
    db_session.add(step)
    await db_session.flush()
    call = AgentToolCall(
        id=str(uuid4()),
        run_id=run.id,
        step_id=step.id,
        logical_call_id=LOGICAL_CALL_ID,
        service="insight-cube-mcp",
        internal_tool_name="query_analysis_data",
        arguments_json={"keyword": "美妆"},
        arguments_hash="a" * 64,
        status=status,
        points_reserved=10,
        points_settled=0,
        upstream_request_id=upstream_request_id,
        started_at=now,
    )
    db_session.add(call)
    await _reserve(call, user_id, db_session)
    await db_session.flush()
    return call


@pytest_asyncio.fixture
async def reconcile_client_factory(
    db_session: AsyncSession,
) -> AsyncIterator[
    Callable[[Any | None], Coroutine[Any, Any, tuple[AsyncClient, User]]]
]:
    """构造一个可注入 payload reconciler 的 admin 客户端。"""
    clients: list[AsyncClient] = []

    async def create(reconciler: Any | None = None) -> tuple[AsyncClient, User]:
        app = create_app()

        async def override_get_db() -> AsyncIterator[AsyncSession]:
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        if reconciler is not None:
            app.state.agent_tool_reconciler = reconciler
        now = _now()
        admin = User(
            id=str(uuid4()),
            nickname="管理员",
            role="admin",
            status="active",
            created_at=now,
            updated_at=now,
        )
        login = LoginSession(
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
        db_session.add(login)
        await db_session.flush()
        token = create_access_token(user_id=admin.id, session_id=login.id, role="admin")
        test_client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        test_client.headers["Authorization"] = f"Bearer {token}"
        clients.append(test_client)
        return test_client, admin

    yield create
    for test_client in clients:
        await test_client.aclose()


async def _payload_reconciler(upstream_request_id: str) -> RemoteToolResult | None:
    return RemoteToolResult(
        structured_content={"result": '{"rows": [{"keyword": "美妆"}]}'},
        is_error=False,
        upstream_request_id=upstream_request_id,
    )


@pytest.mark.asyncio
async def test_reconcile_requires_admin(authed_client_factory, user_factory, db_session) -> None:
    user = await user_factory()
    await WalletService(db_session).ensure_welcome_grant(user.id)
    call = await _make_unknown_call(db_session, user.id)
    user_client, _ = await authed_client_factory(role="user")

    response = await user_client.post(
        f"/api/v1/admin/agent-tool-calls/{call.id}/reconcile",
        json={"decision": "keep_unknown"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_confirm_success_with_payload_creates_evidence_and_settles(
    db_session, user_factory, reconcile_client_factory
) -> None:
    user = await user_factory()
    await WalletService(db_session).ensure_welcome_grant(user.id)
    call = await _make_unknown_call(db_session, user.id)
    admin_client, admin = await reconcile_client_factory(_payload_reconciler)

    response = await admin_client.post(
        f"/api/v1/admin/agent-tool-calls/{call.id}/reconcile",
        json={"decision": "confirm_success", "note": "人工确认成功"},
        headers={"Idempotency-Key": "ik-success"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "settled"
    assert response.json()["evidence_id"] is not None

    evidence = await db_session.scalar(
        select(EvidenceItem).where(EvidenceItem.tool_call_id == call.id)
    )
    assert evidence is not None
    assert evidence.raw_payload_json == {"result": '{"rows": [{"keyword": "美妆"}]}'}

    wallet = await WalletService(db_session).get_wallet(user.id)
    assert wallet.balance == 990
    assert wallet.reserved == 0

    reconciliation = await db_session.scalar(
        select(AgentToolCallReconciliation).where(
            AgentToolCallReconciliation.tool_call_id == call.id
        )
    )
    assert reconciliation.source == "admin"
    assert reconciliation.decision == "confirm_success"
    assert reconciliation.actor_user_id == admin.id
    audit = await db_session.scalar(
        select(AdminAuditLog).where(
            AdminAuditLog.action == "agent_tool_call.reconcile",
            AdminAuditLog.target_id == call.id,
        )
    )
    assert audit is not None


@pytest.mark.asyncio
async def test_confirm_success_without_payload_settles_but_no_evidence(
    db_session, user_factory, reconcile_client_factory
) -> None:
    user = await user_factory()
    await WalletService(db_session).ensure_welcome_grant(user.id)
    call = await _make_unknown_call(db_session, user.id)
    admin_client, _ = await reconcile_client_factory(reconciler=None)  # 无 payload 可取

    response = await admin_client.post(
        f"/api/v1/admin/agent-tool-calls/{call.id}/reconcile",
        json={"decision": "confirm_success"},
        headers={"Idempotency-Key": "ik-no-payload"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "settled"
    assert response.json()["evidence_id"] is None

    # 管理员不能伪造 Evidence：无 payload 绝不创建 Evidence
    evidence = await db_session.scalar(
        select(EvidenceItem).where(EvidenceItem.tool_call_id == call.id)
    )
    assert evidence is None
    # settle 已发生且标记结果不可用
    refreshed = await db_session.get(AgentToolCall, call.id)
    assert refreshed.safe_error_message == "result_unavailable"
    wallet = await WalletService(db_session).get_wallet(user.id)
    assert wallet.balance == 990
    assert wallet.reserved == 0


@pytest.mark.asyncio
async def test_confirm_failure_releases_reservation(
    db_session, user_factory, reconcile_client_factory
) -> None:
    user = await user_factory()
    await WalletService(db_session).ensure_welcome_grant(user.id)
    call = await _make_unknown_call(db_session, user.id)
    admin_client, _ = await reconcile_client_factory()

    response = await admin_client.post(
        f"/api/v1/admin/agent-tool-calls/{call.id}/reconcile",
        json={"decision": "confirm_failure", "note": "确认上游失败"},
        headers={"Idempotency-Key": "ik-failure"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "failed"

    wallet = await WalletService(db_session).get_wallet(user.id)
    assert wallet.balance == 1000
    assert wallet.reserved == 0
    reconciliation = await db_session.scalar(
        select(AgentToolCallReconciliation).where(
            AgentToolCallReconciliation.tool_call_id == call.id
        )
    )
    assert reconciliation.decision == "confirm_failure"


@pytest.mark.asyncio
async def test_keep_unknown_stays_reserved_and_audits(
    db_session, user_factory, reconcile_client_factory
) -> None:
    user = await user_factory()
    await WalletService(db_session).ensure_welcome_grant(user.id)
    call = await _make_unknown_call(db_session, user.id)
    admin_client, admin = await reconcile_client_factory()

    response = await admin_client.post(
        f"/api/v1/admin/agent-tool-calls/{call.id}/reconcile",
        json={"decision": "keep_unknown", "note": "无法核对"},
        headers={"Idempotency-Key": "ik-keep"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "unknown"

    # 保持预留，不动钱包
    wallet = await WalletService(db_session).get_wallet(user.id)
    assert wallet.balance == 990
    assert wallet.reserved == 10
    reconciliation = await db_session.scalar(
        select(AgentToolCallReconciliation).where(
            AgentToolCallReconciliation.tool_call_id == call.id,
            AgentToolCallReconciliation.source == "admin",
        )
    )
    assert reconciliation.decision == "keep_unknown"
    assert reconciliation.actor_user_id == admin.id
    audit = await db_session.scalar(
        select(AdminAuditLog).where(
            AdminAuditLog.action == "agent_tool_call.reconcile",
            AdminAuditLog.target_id == call.id,
        )
    )
    assert audit is not None
    assert audit.detail_json["decision"] == "keep_unknown"


@pytest.mark.asyncio
async def test_reconcile_idempotent_replay_no_double_settle(
    db_session, user_factory, reconcile_client_factory
) -> None:
    user = await user_factory()
    await WalletService(db_session).ensure_welcome_grant(user.id)
    call = await _make_unknown_call(db_session, user.id)
    admin_client, _ = await reconcile_client_factory(_payload_reconciler)

    first = await admin_client.post(
        f"/api/v1/admin/agent-tool-calls/{call.id}/reconcile",
        json={"decision": "confirm_success"},
        headers={"Idempotency-Key": "ik-replay"},
    )
    second = await admin_client.post(
        f"/api/v1/admin/agent-tool-calls/{call.id}/reconcile",
        json={"decision": "confirm_success"},
        headers={"Idempotency-Key": "ik-replay"},
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["status"] == second.json()["status"] == "settled"
    assert first.json()["evidence_id"] == second.json()["evidence_id"]

    # 只结算一次：幂等键阻止重复扣费（settle 幂等键按 permit 维度唯一）
    settles = (
        await db_session.scalars(
            select(TenantWalletTransaction).where(
                TenantWalletTransaction.tool_call_id == call.id,
                TenantWalletTransaction.kind == "settle",
            )
        )
    ).all()
    assert len(settles) == 1
    wallet = await WalletService(db_session).get_wallet(user.id)
    assert wallet.balance == 990
    assert wallet.reserved == 0


@pytest.mark.asyncio
async def test_confirm_success_with_invalid_payload_settles_without_evidence(
    db_session, user_factory, reconcile_client_factory
) -> None:
    """人工取回的 payload 必须重新过输出 Schema 校验（设计 §5.3）：
    校验不过 → 不落 Evidence，退化为 settle + result_unavailable。"""
    async def invalid_payload_reconciler(upstream_request_id: str) -> RemoteToolResult | None:
        return RemoteToolResult(
            structured_content={"wrong_shape": 123},
            is_error=False,
            upstream_request_id=upstream_request_id,
        )

    user = await user_factory()
    await WalletService(db_session).ensure_welcome_grant(user.id)
    call = await _make_unknown_call(db_session, user.id)
    admin_client, _ = await reconcile_client_factory(invalid_payload_reconciler)

    response = await admin_client.post(
        f"/api/v1/admin/agent-tool-calls/{call.id}/reconcile",
        json={"decision": "confirm_success", "note": "人工确认成功"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "settled"
    assert response.json()["evidence_id"] is None

    # 非法 payload 绝不写 Evidence
    evidence = await db_session.scalar(
        select(EvidenceItem).where(EvidenceItem.tool_call_id == call.id)
    )
    assert evidence is None
    refreshed = await db_session.get(AgentToolCall, call.id)
    assert refreshed.safe_error_message == "result_unavailable"
    wallet = await WalletService(db_session).get_wallet(user.id)
    assert (wallet.balance, wallet.reserved) == (990, 0)
