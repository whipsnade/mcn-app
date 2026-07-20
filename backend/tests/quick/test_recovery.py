from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.billing.models import Wallet, WalletTransaction
from app.billing.service import WalletService
from app.identity.models import User
from app.quick.models import QuickMcpCall
from app.quick.service import sweep_stale_quick_calls


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _seed_running_call(db_session, user: User, *, age_seconds: int) -> QuickMcpCall:
    """真实预留 + running 留痕（模拟进程崩溃现场）。"""
    call_id = str(uuid4())
    await WalletService(db_session).reserve(
        user.id,
        10,
        f"quick:{call_id}:reserve",
        call_id,
        reference_type="quick_mcp_call",
    )
    reserve_tx = await db_session.scalar(
        select(WalletTransaction).where(
            WalletTransaction.idempotency_key == f"quick:{call_id}:reserve"
        )
    )
    row = QuickMcpCall(
        id=call_id,
        user_id=user.id,
        feature="top_posts",
        internal_tool_name="datatap.insight.query.raw.posts.v1",
        arguments_json={"target_type": "tag"},
        status="running",
        points_cost=10,
        reserve_transaction_id=reserve_tx.id,
        created_at=_now() - timedelta(seconds=age_seconds),
    )
    db_session.add(row)
    await db_session.flush()
    return row


@pytest.mark.asyncio
async def test_sweep_releases_stale_running_reservation(
    db_session, user_factory
) -> None:
    user = await user_factory()
    wallet_service = WalletService(db_session)
    await wallet_service.ensure_welcome_grant(user.id)
    stale = await _seed_running_call(db_session, user, age_seconds=600)
    fresh = await _seed_running_call(db_session, user, age_seconds=30)

    released = await sweep_stale_quick_calls(db_session, older_than_seconds=300)

    assert released == 1
    wallet = await db_session.get(Wallet, user.id)
    assert wallet.balance == 990  # stale 预留已释放（+10），fresh 仍占 10
    assert wallet.reserved == 10
    assert stale.status == "failed"
    assert stale.error_type == "recovery_released"
    assert stale.completed_at is not None
    release_tx = await db_session.scalar(
        select(WalletTransaction).where(
            WalletTransaction.idempotency_key == f"quick:{stale.id}:release"
        )
    )
    assert release_tx is not None
    assert release_tx.reference_type == "quick_mcp_call"
    assert stale.settlement_transaction_id == release_tx.id
    assert fresh.status == "running"


@pytest.mark.asyncio
async def test_sweep_is_idempotent_via_shared_release_key(
    db_session, user_factory
) -> None:
    user = await user_factory()
    await WalletService(db_session).ensure_welcome_grant(user.id)
    await _seed_running_call(db_session, user, age_seconds=600)

    first = await sweep_stale_quick_calls(db_session, older_than_seconds=300)
    # 第二次清扫：行已 failed，不再命中；即使命中，release 幂等键也去重。
    second = await sweep_stale_quick_calls(db_session, older_than_seconds=300)

    assert (first, second) == (1, 0)
    wallet = await db_session.get(Wallet, user.id)
    assert wallet.balance == 1000
    assert wallet.reserved == 0


@pytest.mark.asyncio
async def test_sweep_ignores_terminal_rows(db_session, user_factory) -> None:
    user = await user_factory()
    await WalletService(db_session).ensure_welcome_grant(user.id)
    row = QuickMcpCall(
        id=str(uuid4()),
        user_id=user.id,
        feature="kol_recommend",
        internal_tool_name="datatap.douyin.kol.search.v1",
        arguments_json={},
        status="succeeded",
        points_cost=10,
        created_at=_now() - timedelta(hours=1),
        completed_at=_now() - timedelta(minutes=59),
    )
    db_session.add(row)
    await db_session.flush()

    released = await sweep_stale_quick_calls(db_session, older_than_seconds=300)

    assert released == 0
    assert row.status == "succeeded"
