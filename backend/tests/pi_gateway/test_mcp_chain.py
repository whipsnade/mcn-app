"""Pi MCP 服务级测试：透明 Tool Result 旁路计费与严格身份边界。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.agent_runtime.models import AgentRun, AgentRunAttempt, AgentSession, AgentStep, AgentToolCall, EvidenceItem
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
    schema = {
        "type": "object",
        "properties": {"keyword": {"type": "string"}},
        "required": ["keyword"],
        "additionalProperties": False,
    }
    db_session.add(
        McpToolCatalog(
            id=str(uuid4()), service_slug="insight-cube-mcp", internal_tool_name="query_analysis_data",
            reviewed_description="品牌声量统计", input_schema_json=schema, output_validator_version="v1",
            discovery_digest=_DIGEST, review_status="approved", is_enabled=True,
            created_at=now, updated_at=now,
        )
    )
    db_session.add(
        McpToolDiscovery(
            id=str(uuid4()), service_slug="insight-cube-mcp", remote_name="query_analysis_data",
            description="品牌声量统计", input_schema_json=schema, output_schema_json=None,
            discovery_digest=_DIGEST, review_status="approved", discovered_at=now, updated_at=now,
        )
    )
    await db_session.flush()


async def _pi_run(
    db_session,
    user,
    *,
    cancel_requested: bool = False,
) -> tuple[AgentRun, AgentRunAttempt, str]:
    membership = await db_session.scalar(select(TenantMembership).where(TenantMembership.user_id == user.id))
    assert membership is not None
    tenant_id = membership.tenant_id
    now = _now()
    session = AgentSession(
        id=str(uuid4()), user_id=user.id, tenant_id=tenant_id, title="pi mcp 链路",
        status="active", created_at=now, updated_at=now,
    )
    db_session.add(session)
    await db_session.flush()
    snapshot = _snapshot()
    snapshot["adapter_catalog"] = [{
        "catalog_entry_id": "placeholder", "adapter_visible_name": "query_analysis_data",
        "service": "insight-cube-mcp", "remote_name": "query_analysis_data",
        "input_schema_digest": f"sha256:{_DIGEST}",
    }]
    run = AgentRun(
        id=str(uuid4()), session_id=session.id, user_id=user.id, tenant_id=tenant_id,
        runtime_backend="pi", runtime_config_version_id=None, runtime_config_snapshot_json=None,
        queued_at=None, profile_name="session_analyst_v1", profile_version="v1", model="fake-model",
        status="running", decision_count=0, review_count=0, revision_count=0,
        created_at=now, started_at=now, run_kind="user", cancel_requested=cancel_requested,
    )
    db_session.add(run)
    await db_session.flush()
    catalog = await db_session.scalar(
        select(McpToolCatalog).where(McpToolCatalog.internal_tool_name == "query_analysis_data")
    )
    snapshot["adapter_catalog"][0]["catalog_entry_id"] = catalog.id
    run.runtime_config_snapshot_json = snapshot
    attempt = AgentRunAttempt(
        id=str(uuid4()), run_id=run.id, attempt=1, started_at=now, outcome="running", decision_count=0,
    )
    db_session.add(attempt)
    await db_session.flush()
    return run, attempt, tenant_id


async def _fund(db_session, tenant_id: str, user_id: str, balance: int = 1000) -> None:
    accounting = TenantAccountingService(db_session)
    await accounting.ensure_tenant_wallet(tenant_id, balance=balance)
    await accounting.ensure_user_quota(tenant_id, user_id, points_limit=100_000)


async def _preflight(db_session, user_factory):
    user = await user_factory()
    await _seed_catalog(db_session)
    run, attempt, tenant_id = await _pi_run(db_session, user)
    await _fund(db_session, tenant_id, user.id)
    service = PiGatewayService(db_session, gateway_id="gw-chain")
    permit = await service.preflight_mcp(
        run,
        PiGatewayMcpPreflightRequest(
            tool_name="query_analysis_data", server="insight-cube-mcp", args={"keyword": "美妆"}
        ),
    )
    return user, run, attempt, tenant_id, service, permit


@pytest.mark.asyncio
async def test_success_metadata_settles_without_interpreting_or_writing_evidence(db_session, user_factory) -> None:
    _user, run, _attempt, tenant_id, service, permit = await _preflight(db_session, user_factory)
    result = await service.finalize_mcp(
        run,
        PiGatewayMcpFinalizeRequest(
            permit_id=permit.permit_id, outcome="succeeded", upstream_request_id="upstream-1",
            response_bytes=120, adapter_version="adapter-v1",
        ),
    )
    call = await db_session.scalar(select(AgentToolCall).where(AgentToolCall.run_id == run.id))
    assert call is not None and call.status == "settled" and call.points_settled == 10 and call.points_reserved == 0
    assert call.upstream_request_id == "upstream-1"
    assert result["status"] == "settled"
    assert await db_session.scalar(select(EvidenceItem.id).where(EvidenceItem.tool_call_id == call.id)) is None
    step = await db_session.get(AgentStep, call.step_id)
    assert step is not None and step.status == "completed"
    wallet = await db_session.get(TenantWallet, tenant_id)
    assert wallet is not None and (wallet.balance, wallet.reserved) == (990, 0)


@pytest.mark.asyncio
async def test_cancel_requested_run_is_rejected_before_mcp_reservation(db_session, user_factory) -> None:
    user = await user_factory()
    await _seed_catalog(db_session)
    run, _attempt, tenant_id = await _pi_run(db_session, user, cancel_requested=True)
    await _fund(db_session, tenant_id, user.id)
    service = PiGatewayService(db_session, gateway_id="gw-cancel")

    with pytest.raises(TenantAccountingError, match="run_cancel_requested"):
        await service.preflight_mcp(
            run,
            PiGatewayMcpPreflightRequest(
                tool_name="query_analysis_data", server="insight-cube-mcp", args={"keyword": "瑞幸咖啡"}
            ),
        )

    assert await db_session.scalar(select(AgentToolCall.id).where(AgentToolCall.run_id == run.id)) is None
    wallet = await db_session.get(TenantWallet, tenant_id)
    assert wallet is not None and (wallet.balance, wallet.reserved) == (1000, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", [{"outcome": "succeeded"}, {"outcome": "succeeded", "response_bytes": 0}])
async def test_empty_text_and_structured_results_share_confirmed_success_accounting(
    db_session, user_factory, metadata
) -> None:
    # The service deliberately cannot observe the model-visible result. Empty,
    # plain text, multiple text blocks, and structuredContent all use this same
    # metadata-only finalize contract.
    _user, run, _attempt, tenant_id, service, permit = await _preflight(db_session, user_factory)
    await service.finalize_mcp(run, PiGatewayMcpFinalizeRequest(permit_id=permit.permit_id, **metadata))
    call = await db_session.scalar(select(AgentToolCall).where(AgentToolCall.run_id == run.id))
    assert call is not None and call.status == "settled" and call.error_type is None
    assert await db_session.scalar(select(EvidenceItem.id).where(EvidenceItem.tool_call_id == call.id)) is None
    wallet = await db_session.get(TenantWallet, tenant_id)
    assert wallet is not None and wallet.reserved == 0


def test_finalize_rejects_business_payload_and_legacy_envelope() -> None:
    for payload in (
        {"permit_id": "p-1", "details": {"mode": "mcpResult"}},
        {"permit_id": "p-1", "outcome": "succeeded", "structuredContent": {"rows": [1]}},
        {"permit_id": "p-1", "outcome": "succeeded", "payload": {"rows": [1]}},
    ):
        with pytest.raises(ValidationError):
            PiGatewayMcpFinalizeRequest.model_validate(payload)


def test_finalize_metadata_has_a_small_bounded_shape() -> None:
    with pytest.raises(ValidationError):
        PiGatewayMcpFinalizeRequest(
            permit_id="p-1", outcome="succeeded", response_bytes=64 * 1024 * 1024 + 1,
        )


@pytest.mark.asyncio
async def test_confirmed_failure_releases_without_evidence(db_session, user_factory) -> None:
    _user, run, _attempt, tenant_id, service, permit = await _preflight(db_session, user_factory)
    await service.fail_mcp(run, permit.permit_id, "failed_confirmed", metadata=None)
    call = await db_session.scalar(select(AgentToolCall).where(AgentToolCall.run_id == run.id))
    assert call is not None and call.status == "failed" and call.points_reserved == 0
    wallet = await db_session.get(TenantWallet, tenant_id)
    assert wallet is not None and wallet.reserved == 0


@pytest.mark.asyncio
async def test_unknown_keeps_reservation_and_records_compact_metadata_json(
    db_session, user_factory
) -> None:
    _user, run, _attempt, tenant_id, service, permit = await _preflight(db_session, user_factory)
    await service.fail_mcp(
        run, permit.permit_id, "result_unknown",
        metadata={
            "version": "mcp_failure_v1",
            "source": "call_failed",
            "error_class": "call_failed",
            "dispatch_phase": "dispatched",
            "upstream_request_id": "req-abc",
        },
    )
    call = await db_session.scalar(select(AgentToolCall).where(AgentToolCall.run_id == run.id))
    assert call is not None and call.status == "unknown" and call.error_type == "result_unknown"
    # 提交 3：safe_error_message 是紧凑 JSON（含 version/source 与可观测字段子集）。
    summary = json.loads(call.safe_error_message.removeprefix("result_unknown:"))
    assert summary["version"] == "mcp_failure_v1"
    assert summary["source"] == "call_failed"
    assert summary["error_class"] == "call_failed"
    assert summary["dispatch_phase"] == "dispatched"
    assert summary["upstream_request_id"] == "req-abc"
    # 分类语义不变：result_unknown 仍保持预留，不释放。
    wallet = await db_session.get(TenantWallet, tenant_id)
    assert wallet is not None and wallet.reserved == 10


@pytest.mark.asyncio
async def test_preflight_rejects_unreviewed_tool_before_reservation(db_session, user_factory) -> None:
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
    assert list((await db_session.scalars(select(AgentToolCall).where(AgentToolCall.run_id == run.id))).all()) == []
    wallet = await db_session.get(TenantWallet, tenant_id)
    assert wallet is not None and (wallet.balance, wallet.reserved) == (1000, 0)
