from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.mcp_gateway.contracts import DataTapService, McpCallStatus
from app.mcp_gateway.fake import FakeMcpTransport
from sqlalchemy import delete, func, select

from app.db.session import SessionFactory
from app.identity.models import User
from app.mcp_gateway.models import McpCall, McpToolCatalog
from app.mcp_gateway.registry import (
    ApprovedTool,
    ToolNotEnabledError,
    ToolRegistryService,
    discovery_digest,
)
from app.mcp_gateway.service import McpCallService
from app.mcp_gateway.transport import (
    DiscoveredTool,
    JsonValue,
    LogicalCallConflictError,
    PossiblySentTimeout,
    RemoteToolResult,
)
from app.tasks.models import AnalysisTask, TaskEvent
from app.workspace.models import Message, WorkspaceSession
from app.mcp_gateway.validation import canonical_json_bytes

from tests.mcp_gateway.fakes import create_analysis_task, strict_object_schema


class InjectedProcessCrash(BaseException):
    pass


INPUT_SCHEMA = strict_object_schema({"keyword": {"type": "string"}}, "keyword")
OUTPUT_SCHEMA = strict_object_schema({"items": {"type": "array", "items": {}}}, "items")


class MemoryArgumentsLoader:
    def __init__(self, values=None) -> None:
        self.values = values or {}
        self.call_count = 0

    async def load_arguments(self, *, task_id: str, plan_step_id: str) -> dict[str, JsonValue]:
        self.call_count += 1
        return self.values[(task_id, plan_step_id)]


class StaticRegistry:
    def __init__(
        self,
        *,
        service: DataTapService = DataTapService.SOCIAL_GROW,
        internal_name: str = "creator.search.v1",
        remote_name: str = "search-creators",
        input_schema=None,
        output_schema=None,
        error: Exception | None = None,
    ) -> None:
        self.approved = ApprovedTool(
            catalog_id="catalog-1",
            internal_name=internal_name,
            service=service,
            remote_name=remote_name,
            input_schema=input_schema or INPUT_SCHEMA,
            output_schema=output_schema or OUTPUT_SCHEMA,
        )
        self.error = error
        self.call_count = 0

    async def require_enabled(self, internal_name: str) -> ApprovedTool:
        self.call_count += 1
        if self.error is not None:
            raise self.error
        if internal_name != self.approved.internal_name:
            raise ToolNotEnabledError("tool is not enabled")
        return self.approved


class BlockingTransport(FakeMcpTransport):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def call_tool(self, service, remote_name, arguments):
        self.call_count += 1
        self.entered.set()
        await self.release.wait()
        return RemoteToolResult(
            structured_content={"items": []},
            is_error=False,
            upstream_request_id="blocking-request",
        )


class CrashingTransport(FakeMcpTransport):
    async def call_tool(
        self,
        service: DataTapService,
        remote_name: str,
        arguments: dict[str, JsonValue],
    ) -> RemoteToolResult:
        self.call_count += 1
        raise InjectedProcessCrash()


class AuditedTransport(FakeMcpTransport):
    def protocol_session_digest(self, service: DataTapService) -> str:
        assert service is DataTapService.SOCIAL_GROW
        return "a" * 64


def _arguments_digest(arguments: dict[str, JsonValue]) -> str:
    return hashlib.sha256(canonical_json_bytes(arguments)).hexdigest()


async def _reserved_call(
    db_session,
    user_factory,
    *,
    arguments=None,
    output_schema=None,
    service: DataTapService = DataTapService.SOCIAL_GROW,
    internal_tool_name: str = "creator.search.v1",
) -> McpCall:
    task = await create_analysis_task(db_session, user_factory)
    now = datetime.now(UTC).replace(tzinfo=None)
    arguments = arguments or {"keyword": "美妆"}
    call = McpCall(
        id=str(uuid4()),
        logical_call_id=str(uuid4()),
        task_id=task.id,
        batch_no=0,
        plan_step_id="step-1",
        attempt=1,
        service_slug=service.value,
        internal_tool_name=internal_tool_name,
        arguments_digest=_arguments_digest(arguments),
        status=McpCallStatus.RESERVED.value,
        evidence_json=None,
        created_at=now,
        updated_at=now,
    )
    db_session.add(call)
    await db_session.flush()
    return call


def _call_service(
    db_session,
    transport,
    call: McpCall,
    *,
    arguments=None,
    registry=None,
    output_schema=None,
) -> McpCallService:
    loader = MemoryArgumentsLoader(
        {(call.task_id, call.plan_step_id): arguments or {"keyword": "美妆"}}
    )
    return McpCallService(
        db_session,
        transport,
        registry=registry or StaticRegistry(output_schema=output_schema),
        arguments_loader=loader,
    )


async def _committed_reserved_call() -> tuple[McpCall, str]:
    async with SessionFactory() as setup:

        async def user_factory() -> User:
            now = datetime.now(UTC).replace(tzinfo=None)
            user = User(
                id=str(uuid4()),
                nickname="独立事务测试",
                role="user",
                status="active",
                created_at=now,
                updated_at=now,
            )
            setup.add(user)
            await setup.flush()
            return user

        call = await _reserved_call(setup, user_factory)
        user_id = await setup.scalar(
            select(AnalysisTask.user_id).where(AnalysisTask.id == call.task_id)
        )
        await setup.commit()
        assert user_id is not None
        return call, user_id


async def _cleanup_committed_call(user_id: str) -> None:
    async with SessionFactory.begin() as cleanup:
        await cleanup.execute(delete(AnalysisTask).where(AnalysisTask.user_id == user_id))
        await cleanup.execute(delete(Message).where(Message.user_id == user_id))
        await cleanup.execute(delete(WorkspaceSession).where(WorkspaceSession.user_id == user_id))
        await cleanup.execute(delete(User).where(User.id == user_id))


async def _committed_analysis_task() -> tuple[AnalysisTask, str]:
    async with SessionFactory() as setup:

        async def user_factory() -> User:
            now = datetime.now(UTC).replace(tzinfo=None)
            user = User(
                id=str(uuid4()),
                nickname="并发 prepare 测试",
                role="user",
                status="active",
                created_at=now,
                updated_at=now,
            )
            setup.add(user)
            await setup.flush()
            return user

        task = await create_analysis_task(setup, user_factory)
        await setup.commit()
        return task, task.user_id


async def _concurrent_prepare(
    *,
    logical_call_id: str,
    task: AnalysisTask,
    arguments: dict[str, JsonValue],
) -> McpCall | Exception:
    async with SessionFactory() as session:
        session.add(
            TaskEvent(
                task_id=task.id,
                user_id=task.user_id,
                event_type="prepare_side_marker",
                payload_json={"marker": arguments["keyword"]},
                created_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )
        service = McpCallService(
            session,
            FakeMcpTransport(),
            registry=StaticRegistry(),
            arguments_loader=MemoryArgumentsLoader(),
        )
        try:
            result = await service.prepare(
                logical_call_id=logical_call_id,
                user_id=task.user_id,
                task_id=task.id,
                plan_step_id="concurrent-step",
                internal_tool_name="creator.search.v1",
                arguments=arguments,
            )
            await session.commit()
            return result
        except LogicalCallConflictError as exc:
            await session.commit()
            return exc
        except Exception as exc:
            await session.rollback()
            return exc


async def test_possible_sent_timeout_becomes_unknown_and_is_not_replayed(
    db_session, user_factory
) -> None:
    reserved_call = await _reserved_call(db_session, user_factory)
    transport = FakeMcpTransport(call_error=PossiblySentTimeout("fake read timeout"))
    service = _call_service(db_session, transport, reserved_call)

    first = await service.invoke(reserved_call.logical_call_id)
    second = await service.invoke(reserved_call.logical_call_id)

    assert first.status == second.status == McpCallStatus.UNKNOWN.value
    assert transport.call_count == 1
    assert first.evidence_json == {"outcome": "unknown"}
    assert "美妆" not in (first.error_message or "")


async def test_running_claim_is_committed_before_network_and_terminal_commits() -> None:
    call, user_id = await _committed_reserved_call()
    logical_call_id = call.logical_call_id
    transport = BlockingTransport()
    try:
        async with SessionFactory() as invoking:
            task = asyncio.create_task(
                _call_service(invoking, transport, call).invoke(logical_call_id)
            )
            await transport.entered.wait()

            async with SessionFactory() as observer:
                visible_status = await observer.scalar(
                    select(McpCall.status).where(McpCall.logical_call_id == logical_call_id)
                )
            assert visible_status == McpCallStatus.RUNNING.value

            transport.release.set()
            result = await task
            assert result.status == McpCallStatus.SUCCEEDED.value

        async with SessionFactory() as observer:
            terminal_status = await observer.scalar(
                select(McpCall.status).where(McpCall.logical_call_id == logical_call_id)
            )
        assert terminal_status == McpCallStatus.SUCCEEDED.value
    finally:
        transport.release.set()
        await _cleanup_committed_call(user_id)


async def test_cancelled_after_network_entry_leaves_running_and_is_not_replayed() -> None:
    call, user_id = await _committed_reserved_call()
    logical_call_id = call.logical_call_id
    transport = BlockingTransport()
    try:
        async with SessionFactory() as invoking:
            task = asyncio.create_task(
                _call_service(invoking, transport, call).invoke(logical_call_id)
            )
            await transport.entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        retry_transport = FakeMcpTransport()
        async with SessionFactory() as retrying:
            result = await _call_service(retrying, retry_transport, call).invoke(logical_call_id)
        assert result.status == McpCallStatus.RUNNING.value
        assert retry_transport.call_count == 0
    finally:
        transport.release.set()
        await _cleanup_committed_call(user_id)


async def test_process_crash_after_claim_leaves_running_and_is_not_replayed() -> None:
    call, user_id = await _committed_reserved_call()
    logical_call_id = call.logical_call_id
    try:
        async with SessionFactory() as invoking:
            with pytest.raises(InjectedProcessCrash):
                await _call_service(invoking, CrashingTransport(), call).invoke(logical_call_id)

        retry_transport = FakeMcpTransport()
        async with SessionFactory() as retrying:
            result = await _call_service(retrying, retry_transport, call).invoke(logical_call_id)
        assert result.status == McpCallStatus.RUNNING.value
        assert retry_transport.call_count == 0
    finally:
        await _cleanup_committed_call(user_id)


async def test_explicit_failure_persists_no_raw_arguments(db_session, user_factory) -> None:
    reserved_call = await _reserved_call(db_session, user_factory)
    transport = FakeMcpTransport(
        call_result=RemoteToolResult(
            structured_content={"error": "denied"},
            is_error=True,
            upstream_request_id="request-1",
        )
    )

    result = await _call_service(db_session, transport, reserved_call).invoke(
        reserved_call.logical_call_id
    )

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
    service = _call_service(
        db_session,
        transport,
        reserved_call,
        output_schema=strict_object_schema({"count": {"type": "integer"}}, "count"),
    )

    first = await service.invoke(reserved_call.logical_call_id)
    second = await service.invoke(reserved_call.logical_call_id)

    assert first.status == second.status == McpCallStatus.FAILED.value
    assert first.evidence_json == {"outcome": "failed"}
    assert transport.call_count == 1


async def test_success_persists_no_raw_arguments_and_is_not_replayed(
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
    service = _call_service(db_session, transport, reserved_call)

    first = await service.invoke(reserved_call.logical_call_id)
    second = await service.invoke(reserved_call.logical_call_id)

    assert first.status == second.status == McpCallStatus.SUCCEEDED.value
    assert "pending_invocation" not in (first.evidence_json or {})
    assert first.response_hash is not None
    assert transport.call_count == 1


async def test_invoke_persists_only_hashed_protocol_session_audit(db_session, user_factory) -> None:
    reserved_call = await _reserved_call(db_session, user_factory)
    transport = AuditedTransport(
        call_result=RemoteToolResult(
            structured_content={"items": []},
            is_error=False,
            upstream_request_id="request-audit",
        )
    )

    result = await _call_service(db_session, transport, reserved_call).invoke(
        reserved_call.logical_call_id
    )

    assert result.protocol_session_digest == "a" * 64
    assert result.evidence_json is not None
    assert "gateway_session_id" not in result.evidence_json
    assert "credential_version" not in result.evidence_json


async def test_invoke_validates_loaded_arguments_before_network(db_session, user_factory) -> None:
    arguments = {"url": "https://evil.invalid"}
    reserved_call = await _reserved_call(db_session, user_factory, arguments=arguments)
    transport = FakeMcpTransport()

    result = await _call_service(db_session, transport, reserved_call, arguments=arguments).invoke(
        reserved_call.logical_call_id
    )

    assert result.status == McpCallStatus.FAILED.value
    assert result.error_type == "input_validation_error"
    assert result.evidence_json == {"outcome": "failed"}
    assert transport.call_count == 0


async def test_invoke_rejects_loaded_arguments_digest_mismatch_before_network(
    db_session, user_factory
) -> None:
    reserved_call = await _reserved_call(db_session, user_factory)
    transport = FakeMcpTransport()

    result = await _call_service(
        db_session,
        transport,
        reserved_call,
        arguments={"keyword": "汽车"},
    ).invoke(reserved_call.logical_call_id)

    assert result.status == McpCallStatus.FAILED.value
    assert result.error_type == "arguments_digest_mismatch"
    assert transport.call_count == 0


async def test_invoke_rejects_registry_binding_mismatch_before_network(
    db_session, user_factory
) -> None:
    reserved_call = await _reserved_call(db_session, user_factory)
    transport = FakeMcpTransport()
    registry = StaticRegistry(service=DataTapService.BILIBILI)

    result = await _call_service(db_session, transport, reserved_call, registry=registry).invoke(
        reserved_call.logical_call_id
    )

    assert result.status == McpCallStatus.FAILED.value
    assert result.error_type == "tool_binding_mismatch"
    assert registry.call_count == 1
    assert transport.call_count == 0


async def test_invoke_rejects_tool_disabled_since_prepare_before_network(
    db_session, user_factory
) -> None:
    reserved_call = await _reserved_call(db_session, user_factory)
    transport = FakeMcpTransport()
    registry = StaticRegistry(error=ToolNotEnabledError("disabled"))

    result = await _call_service(db_session, transport, reserved_call, registry=registry).invoke(
        reserved_call.logical_call_id
    )

    assert result.status == McpCallStatus.FAILED.value
    assert result.error_type == "tool_not_enabled"
    assert registry.call_count == 1
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
    service = McpCallService(
        db_session,
        transport,
        registry=registry,
        arguments_loader=MemoryArgumentsLoader(),
    )
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


async def test_prepare_persists_digest_without_raw_arguments(db_session, user_factory) -> None:
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
        arguments_loader=MemoryArgumentsLoader(),
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

    assert prepared.evidence_json is None
    assert prepared.arguments_digest == _arguments_digest({"keyword": "美妆"})


async def test_concurrent_identical_prepare_returns_same_logical_call() -> None:
    task, user_id = await _committed_analysis_task()
    logical_call_id = str(uuid4())
    try:
        first, second = await asyncio.gather(
            _concurrent_prepare(
                logical_call_id=logical_call_id,
                task=task,
                arguments={"keyword": "美妆"},
            ),
            _concurrent_prepare(
                logical_call_id=logical_call_id,
                task=task,
                arguments={"keyword": "美妆"},
            ),
        )

        assert isinstance(first, McpCall)
        assert isinstance(second, McpCall)
        assert first.id == second.id
        async with SessionFactory() as observer:
            marker_count = await observer.scalar(
                select(func.count(TaskEvent.id)).where(
                    TaskEvent.task_id == task.id,
                    TaskEvent.event_type == "prepare_side_marker",
                )
            )
        assert marker_count == 2
    finally:
        await _cleanup_committed_call(user_id)


async def test_concurrent_conflicting_prepare_maps_to_logical_conflict() -> None:
    task, user_id = await _committed_analysis_task()
    logical_call_id = str(uuid4())
    try:
        results = await asyncio.gather(
            _concurrent_prepare(
                logical_call_id=logical_call_id,
                task=task,
                arguments={"keyword": "美妆"},
            ),
            _concurrent_prepare(
                logical_call_id=logical_call_id,
                task=task,
                arguments={"keyword": "汽车"},
            ),
        )

        assert sum(isinstance(item, McpCall) for item in results) == 1
        assert sum(isinstance(item, LogicalCallConflictError) for item in results) == 1
        async with SessionFactory() as observer:
            marker_count = await observer.scalar(
                select(func.count(TaskEvent.id)).where(
                    TaskEvent.task_id == task.id,
                    TaskEvent.event_type == "prepare_side_marker",
                )
            )
        assert marker_count == 2
    finally:
        await _cleanup_committed_call(user_id)
