from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.mcp_gateway.contracts import DataTapService, McpCallStatus
from app.mcp_gateway.fake import FakeMcpTransport
from app.mcp_gateway.models import McpCall, McpToolCatalog
from app.mcp_gateway.registry import ToolRegistryService, discovery_digest
from app.mcp_gateway.service import McpCallService
from app.mcp_gateway.transport import (
    DiscoveredTool,
    LogicalCallConflictError,
    PossiblySentTimeout,
    RemoteToolResult,
)

from tests.mcp_gateway.fakes import create_analysis_task, strict_object_schema


async def _reserved_call(db_session, user_factory, *, output_schema=None) -> McpCall:
    task = await create_analysis_task(db_session, user_factory)
    now = datetime.now(UTC).replace(tzinfo=None)
    call = McpCall(
        id=str(uuid4()),
        logical_call_id=str(uuid4()),
        task_id=task.id,
        batch_no=0,
        plan_step_id="step-1",
        attempt=1,
        service_slug=DataTapService.SOCIAL_GROW.value,
        internal_tool_name="creator.search.v1",
        arguments_digest="a" * 64,
        status=McpCallStatus.RESERVED.value,
        evidence_json={
            "pending_invocation": {
                "remote_name": "search-creators",
                "pending_arguments": {"keyword": "美妆"},
                "input_schema": strict_object_schema({"keyword": {"type": "string"}}, "keyword"),
                "output_schema": output_schema
                or strict_object_schema({"items": {"type": "array", "items": {}}}, "items"),
            }
        },
        created_at=now,
        updated_at=now,
    )
    db_session.add(call)
    await db_session.flush()
    return call


async def test_possible_sent_timeout_becomes_unknown_and_is_not_replayed(
    db_session, user_factory
) -> None:
    reserved_call = await _reserved_call(db_session, user_factory)
    transport = FakeMcpTransport(call_error=PossiblySentTimeout("fake read timeout"))
    service = McpCallService(db_session, transport)

    first = await service.invoke(reserved_call.logical_call_id)
    second = await service.invoke(reserved_call.logical_call_id)

    assert first.status == second.status == McpCallStatus.UNKNOWN.value
    assert transport.call_count == 1
    assert first.evidence_json == {"outcome": "unknown"}
    assert "美妆" not in (first.error_message or "")


async def test_explicit_failure_clears_pending_arguments(db_session, user_factory) -> None:
    reserved_call = await _reserved_call(db_session, user_factory)
    transport = FakeMcpTransport(
        call_result=RemoteToolResult(
            structured_content={"error": "denied"},
            is_error=True,
            upstream_request_id="request-1",
        )
    )

    result = await McpCallService(db_session, transport).invoke(reserved_call.logical_call_id)

    assert result.status == McpCallStatus.FAILED.value
    assert result.evidence_json == {"outcome": "failed"}
    assert "美妆" not in (result.error_message or "")


async def test_invalid_success_output_is_failed_and_not_replayed(db_session, user_factory) -> None:
    reserved_call = await _reserved_call(
        db_session,
        user_factory,
        output_schema=strict_object_schema({"count": {"type": "integer"}}, "count"),
    )
    transport = FakeMcpTransport(
        call_result=RemoteToolResult(
            structured_content={"count": "not-an-integer"},
            is_error=False,
            upstream_request_id="request-2",
        )
    )
    service = McpCallService(db_session, transport)

    first = await service.invoke(reserved_call.logical_call_id)
    second = await service.invoke(reserved_call.logical_call_id)

    assert first.status == second.status == McpCallStatus.FAILED.value
    assert first.evidence_json == {"outcome": "failed"}
    assert transport.call_count == 1


async def test_success_clears_pending_arguments_and_is_not_replayed(
    db_session, user_factory
) -> None:
    reserved_call = await _reserved_call(db_session, user_factory)
    transport = FakeMcpTransport(
        call_result=RemoteToolResult(
            structured_content={"items": []},
            is_error=False,
            upstream_request_id="request-success",
        )
    )
    service = McpCallService(db_session, transport)

    first = await service.invoke(reserved_call.logical_call_id)
    second = await service.invoke(reserved_call.logical_call_id)

    assert first.status == second.status == McpCallStatus.SUCCEEDED.value
    assert "pending_invocation" not in (first.evidence_json or {})
    assert first.response_hash is not None
    assert transport.call_count == 1


async def test_invoke_revalidates_pending_arguments_before_network(
    db_session, user_factory
) -> None:
    reserved_call = await _reserved_call(db_session, user_factory)
    pending = dict(reserved_call.evidence_json["pending_invocation"])
    pending["pending_arguments"] = {"url": "https://evil.invalid"}
    reserved_call.evidence_json = {"pending_invocation": pending}
    await db_session.flush()
    transport = FakeMcpTransport()

    result = await McpCallService(db_session, transport).invoke(reserved_call.logical_call_id)

    assert result.status == McpCallStatus.FAILED.value
    assert result.error_type == "input_validation_error"
    assert result.evidence_json == {"outcome": "failed"}
    assert transport.call_count == 0


async def test_prepare_is_idempotent_but_conflicting_digest_is_rejected(
    db_session, user_factory
) -> None:
    task = await create_analysis_task(db_session, user_factory)
    input_schema = strict_object_schema({"keyword": {"type": "string"}}, "keyword")
    output_schema = strict_object_schema({"items": {"type": "array", "items": {}}}, "items")
    discovered = DiscoveredTool(
        name="search-creators",
        description="approved",
        input_schema=input_schema,
        output_schema=output_schema,
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    row = McpToolCatalog(
        id=str(uuid4()),
        service_slug=DataTapService.SOCIAL_GROW.value,
        internal_tool_name="creator.search.v1",
        reviewed_description="approved",
        input_schema_json=input_schema,
        output_validator_version="v1",
        discovery_digest=discovery_digest(discovered),
        review_status="approved",
        is_enabled=True,
        created_at=now,
        updated_at=now,
    )
    db_session.add(row)
    await db_session.flush()
    manifest = {
        "manifest_version": 1,
        "tools": [
            {
                "internal_name": row.internal_tool_name,
                "service": row.service_slug,
                "remote_name": discovered.name,
                "description": row.reviewed_description,
                "input_schema": input_schema,
                "output_schema": output_schema,
                "discovery_digest": row.discovery_digest,
            }
        ],
    }
    transport = FakeMcpTransport()
    registry = ToolRegistryService(db_session, transport, manifest=manifest)
    service = McpCallService(db_session, transport, registry=registry)
    logical_call_id = str(uuid4())
    request = {
        "logical_call_id": logical_call_id,
        "user_id": task.user_id,
        "task_id": task.id,
        "plan_step_id": "step-1",
        "internal_tool_name": row.internal_tool_name,
        "arguments": {"keyword": "美妆"},
    }

    first = await service.prepare(**request)
    second = await service.prepare(**request)

    assert first.id == second.id
    assert first.status == McpCallStatus.PLANNED.value
    with pytest.raises(LogicalCallConflictError):
        await service.prepare(**{**request, "arguments": {"keyword": "汽车"}})


async def test_prepare_detaches_persisted_arguments_from_callers_mutable_dict(
    db_session, user_factory
) -> None:
    task = await create_analysis_task(db_session, user_factory)
    input_schema = strict_object_schema({"keyword": {"type": "string"}}, "keyword")
    output_schema = strict_object_schema({"items": {"type": "array", "items": {}}}, "items")
    discovered = DiscoveredTool(
        name="search-creators",
        description="approved",
        input_schema=input_schema,
        output_schema=output_schema,
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    row = McpToolCatalog(
        id=str(uuid4()),
        service_slug=DataTapService.SOCIAL_GROW.value,
        internal_tool_name="creator.search.v1",
        reviewed_description="approved",
        input_schema_json=input_schema,
        output_validator_version="v1",
        discovery_digest=discovery_digest(discovered),
        review_status="approved",
        is_enabled=True,
        created_at=now,
        updated_at=now,
    )
    db_session.add(row)
    await db_session.flush()
    manifest = {
        "manifest_version": 1,
        "tools": [
            {
                "internal_name": row.internal_tool_name,
                "service": row.service_slug,
                "remote_name": discovered.name,
                "description": row.reviewed_description,
                "input_schema": input_schema,
                "output_schema": output_schema,
                "discovery_digest": row.discovery_digest,
            }
        ],
    }
    transport = FakeMcpTransport()
    arguments = {"keyword": "美妆"}
    service = McpCallService(
        db_session,
        transport,
        registry=ToolRegistryService(db_session, transport, manifest=manifest),
    )

    prepared = await service.prepare(
        logical_call_id=str(uuid4()),
        user_id=task.user_id,
        task_id=task.id,
        plan_step_id="step-copy",
        internal_tool_name=row.internal_tool_name,
        arguments=arguments,
    )
    arguments["keyword"] = "汽车"

    pending = prepared.evidence_json["pending_invocation"]
    assert pending["pending_arguments"] == {"keyword": "美妆"}
