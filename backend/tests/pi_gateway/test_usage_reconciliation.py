from sqlalchemy import delete, select
import pytest

from app.billing.models import TenantWallet
from app.pi_gateway.accounting import (
    McpPreflightContext,
    RuntimeUsageService,
    TenantAccountingService,
)
from app.agent_runtime.models import AgentRunAttempt, AgentStep, AgentToolCall
from app.tenancy.models import TenantMembership
from tests.pi_gateway.test_model_usage import _run


def _context(tenant_id: str, user_id: str, run_id: str, call_id: str) -> McpPreflightContext:
    return McpPreflightContext(
        tenant_id=tenant_id,
        user_id=user_id,
        run_id=run_id,
        tool_call_id=call_id,
        internal_tool_name="query_analysis_data",
        service_slug="insight-cube-mcp",
        arguments={"keyword": "usage"},
        feature="brand_analysis",
    )


async def _attach_tool_call(
    db_session,
    run,
    attempt: AgentRunAttempt,
    call_id: str,
    *,
    sequence: int,
    status: str,
    points_reserved: int,
    points_settled: int,
) -> AgentToolCall:
    step = AgentStep(
        id=f"step-{call_id}",
        run_id=run.id,
        attempt_id=attempt.id,
        sequence=sequence,
        step_type="tool_call",
        status="completed",
        created_at=attempt.started_at,
    )
    call = AgentToolCall(
        id=call_id,
        run_id=run.id,
        step_id=step.id,
        logical_call_id=f"logical-{call_id}",
        service="insight_cube",
        internal_tool_name="query_analysis_data",
        arguments_hash="a" * 64,
        status=status,
        points_reserved=points_reserved,
        points_settled=points_settled,
    )
    db_session.add_all([step, call])
    await db_session.flush()
    return call


@pytest.mark.asyncio
async def test_reconciliation_matches_settled_ledger_without_mutating_wallet(db_session, user_factory) -> None:
    user = await user_factory()
    run, attempt, tenant_id = await _run(db_session, user)
    membership = await db_session.scalar(
        select(TenantMembership).where(TenantMembership.user_id == user.id)
    )
    assert membership is not None and membership.tenant_id == tenant_id
    accounting = TenantAccountingService(db_session)
    await accounting.ensure_tenant_wallet(tenant_id, balance=20)
    await accounting.ensure_user_quota(tenant_id, user.id, points_limit=100)
    permit = await accounting.reserve_mcp_call(_context(tenant_id, user.id, run.id, "call-reconcile"))
    await accounting.settle_mcp_call(permit.permit_id, {"mode": "mcpResult"})
    await _attach_tool_call(
        db_session,
        run,
        attempt,
        permit.tool_call_id,
        sequence=1,
        status="settled",
        points_reserved=0,
        points_settled=10,
    )
    wallet = await db_session.get(TenantWallet, tenant_id)
    assert wallet is not None
    before = (wallet.balance, wallet.reserved)

    result = await RuntimeUsageService(db_session).reconcile_run(run.id)

    assert result.reconciliation_status == "match"
    assert result.mcp_settled_points == 10
    assert result.run_reserved_points == 0
    assert wallet.balance == before[0] and wallet.reserved == before[1]


@pytest.mark.asyncio
async def test_unknown_keeps_reserved_and_mismatch_only_marks_result(db_session, user_factory) -> None:
    user = await user_factory()
    run, _attempt, tenant_id = await _run(db_session, user)
    accounting = TenantAccountingService(db_session)
    await accounting.ensure_tenant_wallet(tenant_id, balance=20)
    await accounting.ensure_user_quota(tenant_id, user.id, points_limit=100)
    permit = await accounting.reserve_mcp_call(_context(tenant_id, user.id, run.id, "call-unknown"))
    await accounting.fail_mcp_call(permit.permit_id, "result_unknown")
    wallet = await db_session.get(TenantWallet, tenant_id)
    assert wallet is not None
    wallet.reserved = 0
    await db_session.flush()

    result = await RuntimeUsageService(db_session).reconcile_run(run.id)

    assert result.reconciliation_status == "mismatch"
    assert "tenant_reserved_mismatch" in result.mismatch_codes
    assert wallet.reserved == 0
    assert result.unknown_reserved_points == 10


@pytest.mark.asyncio
async def test_settled_and_unknown_permits_are_reconciled_per_call(db_session, user_factory) -> None:
    user = await user_factory()
    run, attempt, tenant_id = await _run(db_session, user)
    accounting = TenantAccountingService(db_session)
    await accounting.ensure_tenant_wallet(tenant_id, balance=40)
    await accounting.ensure_user_quota(tenant_id, user.id, points_limit=100)
    settled = await accounting.reserve_mcp_call(
        _context(tenant_id, user.id, run.id, "call-settled")
    )
    await accounting.settle_mcp_call(settled.permit_id, {"mode": "mcpResult"})
    await _attach_tool_call(
        db_session,
        run,
        attempt,
        settled.tool_call_id,
        sequence=1,
        status="settled",
        points_reserved=0,
        points_settled=10,
    )
    unknown = await accounting.reserve_mcp_call(
        _context(tenant_id, user.id, run.id, "call-unknown-2")
    )
    await accounting.fail_mcp_call(unknown.permit_id, "result_unknown")
    await _attach_tool_call(
        db_session,
        run,
        attempt,
        unknown.tool_call_id,
        sequence=2,
        status="unknown",
        points_reserved=10,
        points_settled=0,
    )

    result = await RuntimeUsageService(db_session).reconcile_run(run.id)

    assert result.reconciliation_status == "match"
    assert result.mcp_settled_points == 10
    assert result.run_reserved_points == 10
    assert result.unknown_reserved_points == 10


@pytest.mark.asyncio
async def test_orphan_ledger_without_tool_call_is_marked_mismatch(db_session, user_factory) -> None:
    user = await user_factory()
    run, _attempt, tenant_id = await _run(db_session, user)
    accounting = TenantAccountingService(db_session)
    await accounting.ensure_tenant_wallet(tenant_id, balance=20)
    await accounting.ensure_user_quota(tenant_id, user.id, points_limit=100)
    permit = await accounting.reserve_mcp_call(_context(tenant_id, user.id, run.id, "call-orphan"))
    await accounting.settle_mcp_call(permit.permit_id, {"mode": "mcpResult"})

    await db_session.execute(delete(AgentToolCall).where(AgentToolCall.run_id == run.id))
    await db_session.flush()

    result = await RuntimeUsageService(db_session).reconcile_run(run.id)

    assert result.reconciliation_status == "mismatch"
    assert "tool_call_settled_mismatch" in result.mismatch_codes
