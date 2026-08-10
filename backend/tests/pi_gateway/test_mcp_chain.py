"""Pi MCP 链路服务级测试：preflight durable 预留 → finalize → Evidence。

覆盖 B4/B5 契约：成功调用写不可变 Evidence 并结算 10 分；输出 Schema 非法
按 failed_confirmed 释放；成功但无结构化内容按 succeeded_empty 结算但不留
Evidence；未审核工具在 preflight 即阻断（0 外发、0 扣费）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.agent_runtime.models import AgentRun, AgentRunAttempt, AgentSession, AgentToolCall, EvidenceItem
from app.billing.models import TenantWallet
from app.mcp_gateway.models import McpToolCatalog, McpToolDiscovery
from app.pi_gateway.accounting import TenantAccountingError, TenantAccountingService
from app.pi_gateway.contracts import PiGatewayMcpFinalizeRequest, PiGatewayMcpPreflightRequest
from app.pi_gateway.service import PiGatewayService
from app.tenancy.models import TenantMembership

from .test_model_usage import _snapshot


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


_DIGEST = "a" * 64


async def _seed_catalog(db_session) -> None:
    now = _now()
    db_session.add(
        McpToolCatalog(
            id=str(uuid4()),
            service_slug="insight-cube-mcp",
            internal_tool_name="query_analysis_data",
            reviewed_description="品牌声量统计",
            input_schema_json={
                "type": "object",
                "properties": {"keyword": {"type": "string"}},
                "required": ["keyword"],
                "additionalProperties": False,
            },
            output_validator_version="v1",
            discovery_digest=_DIGEST,
            review_status="approved",
            is_enabled=True,
            created_at=now,
            updated_at=now,
        )
    )
    db_session.add(
        McpToolDiscovery(
            id=str(uuid4()),
            service_slug="insight-cube-mcp",
            remote_name="query_analysis_data",
            description="品牌声量统计",
            input_schema_json={
                "type": "object",
                "properties": {"keyword": {"type": "string"}},
                "required": ["keyword"],
                "additionalProperties": False,
            },
            output_schema_json=None,
            discovery_digest=_DIGEST,
            review_status="approved",
            discovered_at=now,
            updated_at=now,
        )
    )
    await db_session.flush()


async def _pi_run(db_session, user) -> tuple[AgentRun, AgentRunAttempt, str]:
    membership = await db_session.scalar(
        select(TenantMembership).where(TenantMembership.user_id == user.id)
    )
    assert membership is not None
    tenant_id = membership.tenant_id
    now = _now()
    session = AgentSession(
        id=str(uuid4()), user_id=user.id, tenant_id=tenant_id,
        title="pi mcp 链路", status="active", created_at=now, updated_at=now,
    )
    db_session.add(session)
    await db_session.flush()
    snapshot = _snapshot()
    snapshot["adapter_catalog"] = [
        {
            "catalog_entry_id": "placeholder",
            "adapter_visible_name": "query_analysis_data",
            "service": "insight-cube-mcp",
            "remote_name": "query_analysis_data",
            "input_schema_digest": f"sha256:{_DIGEST}",
        }
    ]
    run = AgentRun(
        id=str(uuid4()), session_id=session.id, user_id=user.id, tenant_id=tenant_id,
        runtime_backend="pi", runtime_config_version_id=None,
        runtime_config_snapshot_json=None, queued_at=None,
        profile_name="session_analyst_v1", profile_version="v1", model="fake-model",
        status="running", decision_count=0, review_count=0, revision_count=0,
        created_at=now, started_at=now, run_kind="user",
    )
    db_session.add(run)
    await db_session.flush()
    # 快照里的 catalog_entry_id 必须与真实目录行一致（claim 时绑定的复核口径）。
    catalog = await db_session.scalar(
        select(McpToolCatalog).where(McpToolCatalog.internal_tool_name == "query_analysis_data")
    )
    snapshot["adapter_catalog"][0]["catalog_entry_id"] = catalog.id
    run.runtime_config_snapshot_json = snapshot
    attempt = AgentRunAttempt(
        id=str(uuid4()), run_id=run.id, attempt=1, started_at=now,
        outcome="running", decision_count=0,
    )
    db_session.add(attempt)
    await db_session.flush()
    return run, attempt, tenant_id


async def _fund(db_session, tenant_id: str, user_id: str, balance: int = 1000) -> None:
    accounting = TenantAccountingService(db_session)
    await accounting.ensure_tenant_wallet(tenant_id, balance=balance)
    await accounting.ensure_user_quota(tenant_id, user_id, points_limit=100_000)


@pytest.mark.asyncio
async def test_finalize_writes_evidence_and_settles_after_durable_preflight(
    db_session, user_factory
) -> None:
    user = await user_factory()
    await _seed_catalog(db_session)
    run, _attempt, tenant_id = await _pi_run(db_session, user)
    await _fund(db_session, tenant_id, user.id)
    service = PiGatewayService(db_session, gateway_id="gw-chain")

    permit = await service.preflight_mcp(
        run,
        PiGatewayMcpPreflightRequest(
            tool_name="query_analysis_data", server="insight-cube-mcp", args={"keyword": "美妆"}
        ),
    )
    call = await db_session.scalar(
        select(AgentToolCall).where(AgentToolCall.run_id == run.id)
    )
    assert call is not None and call.status == "running" and call.points_reserved == 10
    wallet = await db_session.get(TenantWallet, tenant_id)
    assert wallet is not None and (wallet.balance, wallet.reserved) == (990, 10)

    payload = {"result": '{"rows": [{"日期": "2026-08-01", "平台": "小红书", "声量": 120}]}'}
    await service.finalize_mcp(
        run,
        PiGatewayMcpFinalizeRequest(
            permit_id=permit.permit_id,
            details={"mode": "call", "mcpResult": {"structuredContent": payload}},
        ),
    )
    await db_session.refresh(call)
    assert call.status == "settled" and call.points_settled == 10
    evidence = await db_session.scalar(
        select(EvidenceItem).where(EvidenceItem.tool_call_id == call.id)
    )
    assert evidence is not None
    assert evidence.raw_payload_json == payload
    assert evidence.source_name == "query_analysis_data"
    await db_session.refresh(wallet)
    assert (wallet.balance, wallet.reserved) == (990, 0)


@pytest.mark.asyncio
async def test_finalize_with_invalid_output_releases_and_writes_no_evidence(
    db_session, user_factory
) -> None:
    user = await user_factory()
    await _seed_catalog(db_session)
    run, _attempt, tenant_id = await _pi_run(db_session, user)
    await _fund(db_session, tenant_id, user.id)
    service = PiGatewayService(db_session, gateway_id="gw-chain")

    permit = await service.preflight_mcp(
        run,
        PiGatewayMcpPreflightRequest(
            tool_name="query_analysis_data", server="insight-cube-mcp", args={"keyword": "美妆"}
        ),
    )
    await service.finalize_mcp(
        run,
        PiGatewayMcpFinalizeRequest(
            permit_id=permit.permit_id,
            details={"mode": "call", "mcpResult": {"structuredContent": {"unexpected": 1}}},
        ),
    )
    call = await db_session.scalar(select(AgentToolCall).where(AgentToolCall.run_id == run.id))
    assert call is not None and call.status == "failed"
    assert call.error_type == "failed_confirmed"
    evidence = await db_session.scalar(select(EvidenceItem).where(EvidenceItem.tool_call_id == call.id))
    assert evidence is None
    wallet = await db_session.get(TenantWallet, tenant_id)
    assert wallet is not None and (wallet.balance, wallet.reserved) == (1000, 0)


@pytest.mark.asyncio
async def test_finalize_without_structured_content_settles_empty_without_evidence(
    db_session, user_factory
) -> None:
    user = await user_factory()
    await _seed_catalog(db_session)
    run, _attempt, tenant_id = await _pi_run(db_session, user)
    await _fund(db_session, tenant_id, user.id)
    service = PiGatewayService(db_session, gateway_id="gw-chain")

    permit = await service.preflight_mcp(
        run,
        PiGatewayMcpPreflightRequest(
            tool_name="query_analysis_data", server="insight-cube-mcp", args={"keyword": "美妆"}
        ),
    )
    await service.finalize_mcp(
        run,
        PiGatewayMcpFinalizeRequest(permit_id=permit.permit_id, details={"mode": "call"}),
    )
    call = await db_session.scalar(select(AgentToolCall).where(AgentToolCall.run_id == run.id))
    assert call is not None and call.status == "failed" and call.error_type == "succeeded_empty"
    assert call.points_settled == 10  # 上游确实成功：结算但不产 Evidence
    evidence = await db_session.scalar(select(EvidenceItem).where(EvidenceItem.tool_call_id == call.id))
    assert evidence is None
    wallet = await db_session.get(TenantWallet, tenant_id)
    assert wallet is not None and (wallet.balance, wallet.reserved) == (990, 0)


@pytest.mark.asyncio
async def test_preflight_rejects_unreviewed_tool_before_any_reservation(
    db_session, user_factory
) -> None:
    user = await user_factory()
    await _seed_catalog(db_session)
    run, _attempt, tenant_id = await _pi_run(db_session, user)
    await _fund(db_session, tenant_id, user.id)
    service = PiGatewayService(db_session, gateway_id="gw-chain")

    with pytest.raises(TenantAccountingError, match="mcp_tool_not_allowed"):
        await service.preflight_mcp(
            run,
            PiGatewayMcpPreflightRequest(
                tool_name="unreviewed_tool", server="insight-cube-mcp", args={"keyword": "美妆"}
            ),
        )
    calls = list((await db_session.scalars(select(AgentToolCall).where(AgentToolCall.run_id == run.id))).all())
    assert calls == []
    wallet = await db_session.get(TenantWallet, tenant_id)
    assert wallet is not None and (wallet.balance, wallet.reserved) == (1000, 0)
