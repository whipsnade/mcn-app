"""Tenant-ledger guard tests for the current runtime MCP path (B4 invariants).

After B4 the tenant wallet is the single source of truth for both runtimes.
A missing TenantWallet must fail closed with zero legacy ``wallets`` writes,
and every external dispatch re-checks membership, License status/window,
feature, quota and tenant wallet.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.agent_runtime.models import (
    AgentRun,
    AgentRunAttempt,
    AgentSession,
    AgentStep,
    AgentToolCall,
    AgentToolCallReconciliation,
    EvidenceItem,
)
from app.agent_runtime.tools.contracts import ToolContext
from app.billing.models import (
    TenantUserQuotaPolicy,
    TenantUserQuotaUsage,
    TenantWallet,
    TenantWalletTransaction,
    Wallet,
    WalletTransaction,
)
from app.db.session import SessionFactory
from app.identity.models import User
from app.licensing.models import TenantLicense
from app.mcp_gateway.transport import McpConnectionTimeout
from app.tenancy.models import Tenant, TenantMembership

from .test_mcp import FakeMcpTransport, _bridge, _ok_result


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass(frozen=True)
class _TenantChain:
    user_id: str
    tenant_id: str
    session_id: str
    run_id: str
    step_id: str


async def _provision_tenant_chain(
    *,
    wallet_balance: int | None = 1000,
    legacy_balance: int | None = None,
    license_status: str = "active",
    features: dict[str, bool] | None = None,
) -> _TenantChain:
    """Committed fixture: user + tenant + license + membership + ledgers."""
    now = _now()
    async with SessionFactory.begin() as db:
        user = User(
            id=str(uuid4()), nickname="租户护栏用户", role="user",
            status="active", created_at=now, updated_at=now,
        )
        db.add(user)
        await db.flush()
        tenant = Tenant(
            id=str(uuid4()), slug=f"guard-{uuid4().hex[:20]}", name="护栏租户",
            status="active", is_internal=False, runtime_backend="current",
            license_status=license_status, active_license_id=None,
            created_at=now, updated_at=now,
        )
        db.add(tenant)
        await db.flush()
        license_row = TenantLicense(
            id=str(uuid4()), tenant_id=tenant.id, version=1,
            valid_from=now.replace(microsecond=0), valid_until=None,
            features_json=features
            or {
                "kol_selection": True,
                "brand_analysis": True,
                "campaign_analysis": True,
                "kol_detail": True,
                "utility": True,
            },
            max_concurrent_runs=10, max_user_concurrent_runs=5,
            created_by=user.id, created_at=now,
        )
        db.add(license_row)
        await db.flush()
        tenant.active_license_id = license_row.id
        db.add(
            TenantMembership(
                id=str(uuid4()), tenant_id=tenant.id, user_id=user.id,
                role="owner", status="active", created_at=now, updated_at=now,
            )
        )
        if wallet_balance is not None:
            db.add(
                TenantWallet(
                    tenant_id=tenant.id, balance=wallet_balance, reserved=0,
                    version=0, updated_at=now,
                )
            )
        db.add(
            TenantUserQuotaPolicy(
                id=str(uuid4()), tenant_id=tenant.id, user_id=user.id,
                period="monthly", points_limit=1_000_000, status="active",
                created_at=now, updated_at=now,
            )
        )
        if legacy_balance is not None:
            db.add(
                Wallet(
                    user_id=user.id, balance=legacy_balance, reserved=0,
                    version=0, updated_at=now,
                )
            )
        session = AgentSession(
            id=str(uuid4()), user_id=user.id, tenant_id=tenant.id,
            title="护栏会话", status="active", created_at=now, updated_at=now,
        )
        db.add(session)
        await db.flush()
        run = AgentRun(
            id=str(uuid4()), session_id=session.id, user_id=user.id,
            tenant_id=tenant.id, run_kind="user", visibility="user",
            profile_name="session_analyst_v1", profile_version="v1", model="test-model",
            status="running", decision_count=0, review_count=0, revision_count=0,
            started_at=now,
        )
        db.add(run)
        await db.flush()
        attempt = AgentRunAttempt(
            id=str(uuid4()), run_id=run.id, attempt=1, started_at=now,
            decision_count=0, outcome="running",
        )
        db.add(attempt)
        await db.flush()
        step = AgentStep(
            id=str(uuid4()), run_id=run.id, attempt_id=attempt.id, sequence=1,
            step_type="tool_call", status="running", visibility="user", created_at=now,
        )
        db.add(step)
        await db.flush()
        return _TenantChain(
            user_id=user.id,
            tenant_id=tenant.id,
            session_id=session.id,
            run_id=run.id,
            step_id=step.id,
        )


async def _teardown_tenant_chain(chain: _TenantChain) -> None:
    async with SessionFactory.begin() as db:
        call_ids = list(
            (await db.scalars(select(AgentToolCall.id).where(AgentToolCall.run_id == chain.run_id))).all()
        )
        for call_id in call_ids:
            for row in (
                await db.scalars(
                    select(AgentToolCallReconciliation).where(
                        AgentToolCallReconciliation.tool_call_id == call_id
                    )
                )
            ).all():
                await db.delete(row)
            for row in (
                await db.scalars(select(EvidenceItem).where(EvidenceItem.tool_call_id == call_id))
            ).all():
                await db.delete(row)
            call = await db.get(AgentToolCall, call_id)
            if call is not None:
                await db.delete(call)
        for model, key, value in (
            (AgentStep, "run_id", chain.run_id),
            (AgentRunAttempt, "run_id", chain.run_id),
            (AgentRun, "id", chain.run_id),
            (AgentSession, "id", chain.session_id),
            (TenantUserQuotaUsage, "tenant_id", chain.tenant_id),
            (TenantUserQuotaPolicy, "tenant_id", chain.tenant_id),
            (TenantWalletTransaction, "tenant_id", chain.tenant_id),
            (TenantWallet, "tenant_id", chain.tenant_id),
            (TenantMembership, "tenant_id", chain.tenant_id),
            (TenantLicense, "tenant_id", chain.tenant_id),
            (Tenant, "id", chain.tenant_id),
            (WalletTransaction, "user_id", chain.user_id),
            (Wallet, "user_id", chain.user_id),
            (User, "id", chain.user_id),
        ):
            for row in (await db.scalars(select(model).where(getattr(model, key) == value))).all():
                await db.delete(row)


def _context(chain: _TenantChain) -> ToolContext:
    return ToolContext(
        user_id=chain.user_id,
        session_id=chain.session_id,
        run_id=chain.run_id,
        profile_name="session_analyst_v1",
        step_id=chain.step_id,
    )


async def _tenant_wallet(tenant_id: str) -> TenantWallet | None:
    async with SessionFactory() as db:
        return await db.get(TenantWallet, tenant_id)


async def _legacy_ledger_rows(user_id: str) -> tuple[int, int]:
    async with SessionFactory() as db:
        wallet = await db.get(Wallet, user_id)
        transactions = list(
            (await db.scalars(select(WalletTransaction).where(WalletTransaction.user_id == user_id))).all()
        )
        return (0 if wallet is None else wallet.balance, len(transactions))


@pytest.mark.asyncio
async def test_missing_tenant_wallet_fails_closed_without_legacy_write() -> None:
    chain = await _provision_tenant_chain(wallet_balance=None)
    try:
        transport = FakeMcpTransport([McpConnectionTimeout("must not dispatch")])
        bridge = _bridge(transport)

        result = await bridge.execute(_context(chain), {"keyword": "美妆"})

        assert result.status == "failed"
        assert result.error_type == "definitely_not_sent"
        assert transport.calls == []
        # 无残留 planned/running 行（prepare 整体回滚）
        async with SessionFactory() as db:
            rows = list(
                (await db.scalars(select(AgentToolCall).where(AgentToolCall.run_id == chain.run_id))).all()
            )
            assert rows == []
        # 旧账表零写入
        assert await _legacy_ledger_rows(chain.user_id) == (0, 0)
    finally:
        await _teardown_tenant_chain(chain)


@pytest.mark.asyncio
async def test_split_brain_legacy_balance_is_never_spendable() -> None:
    """A legacy Wallet balance without a TenantWallet must not fund dispatch."""
    chain = await _provision_tenant_chain(wallet_balance=None, legacy_balance=10_000)
    try:
        transport = FakeMcpTransport([_ok_result()])
        bridge = _bridge(transport)

        result = await bridge.execute(_context(chain), {"keyword": "美妆"})

        assert result.status == "failed"
        assert result.error_type == "definitely_not_sent"
        assert transport.calls == []
        # 旧余额原样保留、零旧流水：它只是迁移审计对象。
        assert await _legacy_ledger_rows(chain.user_id) == (10_000, 0)
    finally:
        await _teardown_tenant_chain(chain)


@pytest.mark.asyncio
async def test_license_suspended_mid_run_blocks_the_next_dispatch() -> None:
    chain = await _provision_tenant_chain()
    try:
        transport = FakeMcpTransport([_ok_result(), _ok_result("req-second")])
        bridge = _bridge(transport)

        first = await bridge.execute(_context(chain), {"keyword": "美妆"})
        assert first.status == "success"
        assert len(transport.calls) == 1

        async with SessionFactory.begin() as db:
            tenant = await db.get(Tenant, chain.tenant_id)
            assert tenant is not None
            tenant.license_status = "suspended"

        second = await bridge.execute(_context(chain), {"keyword": "口红"})
        assert second.status == "failed"
        assert second.error_type == "definitely_not_sent"
        assert "license" in (second.safe_summary or "")
        assert len(transport.calls) == 1  # 第二次外发被阻断：0 新增 dispatch

        wallet = await _tenant_wallet(chain.tenant_id)
        assert wallet is not None
        assert (wallet.balance, wallet.reserved) == (990, 0)  # 仅第一次结算
    finally:
        await _teardown_tenant_chain(chain)


@pytest.mark.asyncio
async def test_feature_disabled_blocks_before_any_dispatch() -> None:
    chain = await _provision_tenant_chain(
        features={
            "kol_selection": False,
            "brand_analysis": True,
            "campaign_analysis": True,
            "kol_detail": True,
            "utility": True,
        }
    )
    try:
        transport = FakeMcpTransport([_ok_result()])
        bridge = _bridge(transport)

        result = await bridge.execute(_context(chain), {"keyword": "美妆"})

        assert result.status == "failed"
        assert result.error_type == "definitely_not_sent"
        assert transport.calls == []
        wallet = await _tenant_wallet(chain.tenant_id)
        assert wallet is not None and (wallet.balance, wallet.reserved) == (1000, 0)
    finally:
        await _teardown_tenant_chain(chain)
