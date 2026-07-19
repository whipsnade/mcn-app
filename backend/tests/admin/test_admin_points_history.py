from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.billing.models import WalletTransaction
from app.mcp_gateway.models import McpCall
from app.tasks.models import AnalysisTask
from app.workspace.models import Message, WorkspaceSession


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _seed_settled_call(db_session, user, *, platform: str) -> WalletTransaction:
    now = utc_now()
    session = WorkspaceSession(
        id=str(uuid4()),
        user_id=user.id,
        title="双11 投放分析",
        brand="测试品牌",
        campaign_name=None,
        status="completed",
        platforms=[platform],
        category="美妆",
        target_audience="受众",
        budget_min=None,
        budget_max=None,
        filters_snapshot={},
        is_starred=False,
        last_accessed_at=now,
        deleted_at=None,
        created_at=now,
        updated_at=now,
    )
    message = Message(
        id=str(uuid4()),
        session_id=session.id,
        user_id=user.id,
        role="user",
        content="帮我分析",
        sequence=1,
        metadata_json={},
        created_at=now,
    )
    task = AnalysisTask(
        id=str(uuid4()),
        user_id=user.id,
        session_id=session.id,
        trigger_message_id=message.id,
        status="completed",
        kind="pipeline",
        created_at=now,
        updated_at=now,
    )
    transaction = WalletTransaction(
        id=str(uuid4()),
        user_id=user.id,
        kind="settle",
        balance_delta=0,
        reserved_delta=-10,
        balance_after=990,
        reserved_after=0,
        idempotency_key=f"mcp:{uuid4()}:settle",
        reference_type="mcp_call",
        reference_id="call-1",
        created_at=now,
    )
    call = McpCall(
        id=str(uuid4()),
        logical_call_id=str(uuid4()),
        task_id=task.id,
        batch_no=1,
        plan_step_id="step-1",
        attempt=1,
        service_slug="bilibili-mcp",
        internal_tool_name="kol.search",
        arguments_digest=uuid4().hex + uuid4().hex,
        status="settled",
        settlement_transaction_id=transaction.id,
        created_at=now,
        updated_at=now,
    )
    # Flush in dependency order: without ORM relationships the unit of work
    # does not guarantee parent rows are inserted before their children.
    db_session.add(session)
    await db_session.flush()
    db_session.add(message)
    await db_session.flush()
    db_session.add(task)
    await db_session.flush()
    db_session.add(transaction)
    await db_session.flush()
    db_session.add(call)
    await db_session.flush()
    return transaction


@pytest.mark.asyncio
async def test_points_history_resolves_session_context(
    authed_client_factory, user_factory, db_session
) -> None:
    admin_client, _ = await authed_client_factory()
    user = await user_factory()
    transaction = await _seed_settled_call(db_session, user, platform="bilibili")

    response = await admin_client.get(f"/api/v1/admin/users/{user.id}/points-history")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    [entry] = body["items"]
    assert entry["id"] == transaction.id
    assert entry["kind"] == "settle"
    assert entry["points"] == 10
    assert entry["session_title"] == "双11 投放分析"
    assert entry["platform"] == "bilibili"


@pytest.mark.asyncio
async def test_points_history_includes_admin_adjust_without_session(
    authed_client_factory, user_factory, db_session
) -> None:
    admin_client, _ = await authed_client_factory()
    user = await user_factory()
    await _seed_settled_call(db_session, user, platform="douyin")

    adjusted = await admin_client.post(
        f"/api/v1/admin/users/{user.id}/points",
        json={"delta": 500, "reason": "人工充值"},
    )
    assert adjusted.status_code == 200

    response = await admin_client.get(f"/api/v1/admin/users/{user.id}/points-history")
    body = response.json()
    assert body["total"] == 2
    kinds = {entry["kind"] for entry in body["items"]}
    assert kinds == {"settle", "admin_adjust"}
    admin_entry = next(item for item in body["items"] if item["kind"] == "admin_adjust")
    assert admin_entry["points"] == 500
    assert admin_entry["session_title"] is None
    assert admin_entry["platform"] is None

    missing = await admin_client.get("/api/v1/admin/users/no-such-user/points-history")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "USER_NOT_FOUND"
