from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp_gateway.contracts import McpCallStatus
from app.mcp_gateway.models import McpCall
from app.mcp_gateway.registry import ToolNotEnabledError, ToolRegistryService
from app.mcp_gateway.transport import (
    JsonValue,
    LogicalCallConflictError,
    McpTransport,
    PossiblySentTimeout,
)
from app.mcp_gateway.validation import (
    McpValidationError,
    canonical_json_bytes,
    validate_input,
    validate_output,
)
from app.tasks.models import AnalysisTask


class McpArgumentsLoader(Protocol):
    async def load_arguments(self, *, task_id: str, plan_step_id: str) -> dict[str, JsonValue]: ...


class McpCallService:
    def __init__(
        self,
        db_session: AsyncSession,
        transport: McpTransport,
        *,
        arguments_loader: McpArgumentsLoader,
        registry: ToolRegistryService | None = None,
    ) -> None:
        self._db = db_session
        self._transport = transport
        self._registry = registry or ToolRegistryService(db_session, transport)
        self._arguments_loader = arguments_loader

    async def prepare(
        self,
        *,
        logical_call_id: str,
        user_id: str,
        task_id: str,
        plan_step_id: str,
        internal_tool_name: str,
        arguments: dict[str, JsonValue],
    ) -> McpCall:
        arguments_digest = hashlib.sha256(canonical_json_bytes(arguments)).hexdigest()
        existing = await self._by_logical_id(logical_call_id)
        if existing is not None:
            if not await self._matches_request(
                existing,
                user_id=user_id,
                task_id=task_id,
                plan_step_id=plan_step_id,
                internal_tool_name=internal_tool_name,
                arguments_digest=arguments_digest,
            ):
                raise LogicalCallConflictError(
                    "logical_call_id already exists with a different request"
                )
            return existing

        task_user_id = await self._db.scalar(
            select(AnalysisTask.user_id).where(AnalysisTask.id == task_id)
        )
        if task_user_id != user_id:
            raise ValueError("analysis task does not belong to user")
        approved = await self._registry.require_enabled(internal_tool_name)
        validate_input(arguments, approved.input_schema)
        now = datetime.now(UTC).replace(tzinfo=None)
        row = McpCall(
            id=str(uuid4()),
            logical_call_id=logical_call_id,
            task_id=task_id,
            batch_no=0,
            plan_step_id=plan_step_id,
            attempt=1,
            service_slug=approved.service.value,
            internal_tool_name=approved.internal_name,
            arguments_digest=arguments_digest,
            status=McpCallStatus.PLANNED.value,
            evidence_json=None,
            created_at=now,
            updated_at=now,
        )
        try:
            async with self._db.begin_nested():
                self._db.add(row)
                await self._db.flush()
        except IntegrityError as exc:
            # A concurrent request may win either unique constraint after our
            # initial lookup. The SAVEPOINT preserves unrelated caller writes.
            winner = await self._by_logical_id(logical_call_id, for_update=True)
            if winner is not None and await self._matches_request(
                winner,
                user_id=user_id,
                task_id=task_id,
                plan_step_id=plan_step_id,
                internal_tool_name=internal_tool_name,
                arguments_digest=arguments_digest,
            ):
                return winner
            raise LogicalCallConflictError(
                "logical MCP call conflicts with an existing request"
            ) from exc
        return row

    async def invoke(self, logical_call_id: str) -> McpCall:
        now = datetime.now(UTC).replace(tzinfo=None)
        claimed = await self._db.execute(
            update(McpCall)
            .where(
                McpCall.logical_call_id == logical_call_id,
                McpCall.status == McpCallStatus.RESERVED.value,
            )
            .values(
                status=McpCallStatus.RUNNING.value,
                started_at=now,
                updated_at=now,
            )
        )
        await self._db.flush()
        row = await self._by_logical_id(logical_call_id, refresh=True)
        await self._db.commit()
        if row is None:
            raise LookupError("MCP logical call does not exist")
        if claimed.rowcount != 1:
            return row

        try:
            approved = await self._registry.require_enabled(row.internal_tool_name)
        except ToolNotEnabledError:
            return await self._finish_failed(row, "tool_not_enabled")
        if (
            approved.internal_name != row.internal_tool_name
            or approved.service.value != row.service_slug
            or not approved.remote_name
        ):
            return await self._finish_failed(row, "tool_binding_mismatch")

        try:
            arguments = await self._arguments_loader.load_arguments(
                task_id=row.task_id,
                plan_step_id=row.plan_step_id,
            )
        except Exception:
            return await self._finish_failed(row, "arguments_unavailable")
        try:
            validated_arguments = validate_input(arguments, approved.input_schema)
        except McpValidationError:
            return await self._finish_failed(row, "input_validation_error")
        loaded_digest = hashlib.sha256(canonical_json_bytes(validated_arguments)).hexdigest()
        if loaded_digest != row.arguments_digest:
            return await self._finish_failed(row, "arguments_digest_mismatch")
        try:
            protocol_digest = self._transport.protocol_session_digest(approved.service)
        except Exception:
            return await self._finish_failed(row, "protocol_session_audit_error")
        if protocol_digest is not None:
            if (
                not isinstance(protocol_digest, str)
                or len(protocol_digest) != 64
                or any(character not in "0123456789abcdef" for character in protocol_digest)
            ):
                return await self._finish_failed(row, "protocol_session_audit_error")
            row.protocol_session_digest = protocol_digest

        # Registry and argument loading may open transactions. Close all database work
        # before the potentially non-idempotent external call.
        await self._db.commit()

        try:
            result = await self._transport.call_tool(
                approved.service, approved.remote_name, validated_arguments
            )
        except PossiblySentTimeout:
            return await self._finish_unknown(row)
        except Exception:
            return await self._finish_failed(row, "upstream_error")

        if result.is_error:
            row.upstream_request_id = result.upstream_request_id
            return await self._finish_failed(row, "upstream_tool_error")
        try:
            validated_output = validate_output(result.structured_content, approved.output_schema)
        except McpValidationError:
            row.upstream_request_id = result.upstream_request_id
            return await self._finish_failed(row, "output_validation_error")

        row.status = McpCallStatus.SUCCEEDED.value
        row.upstream_request_id = result.upstream_request_id
        row.response_hash = hashlib.sha256(canonical_json_bytes(validated_output)).hexdigest()
        row.evidence_json = {
            "outcome": "succeeded",
            "structured_content": validated_output,
            "upstream_request_id": result.upstream_request_id,
        }
        row.error_type = None
        row.error_message = None
        row.completed_at = datetime.now(UTC).replace(tzinfo=None)
        row.updated_at = row.completed_at
        await self._db.commit()
        return row

    async def _finish_unknown(self, row: McpCall) -> McpCall:
        row.status = McpCallStatus.UNKNOWN.value
        row.evidence_json = {"outcome": "unknown"}
        row.error_type = "possibly_sent_timeout"
        row.error_message = "MCP outcome could not be confirmed"
        row.completed_at = datetime.now(UTC).replace(tzinfo=None)
        row.updated_at = row.completed_at
        await self._db.commit()
        return row

    async def _finish_failed(self, row: McpCall, error_type: str) -> McpCall:
        row.status = McpCallStatus.FAILED.value
        row.evidence_json = {"outcome": "failed"}
        row.error_type = error_type
        row.error_message = "MCP call failed"
        row.completed_at = datetime.now(UTC).replace(tzinfo=None)
        row.updated_at = row.completed_at
        await self._db.commit()
        return row

    async def _by_logical_id(
        self,
        logical_call_id: str,
        *,
        refresh: bool = False,
        for_update: bool = False,
    ) -> McpCall | None:
        statement = select(McpCall).where(McpCall.logical_call_id == logical_call_id)
        if for_update:
            statement = statement.with_for_update()
        row = await self._db.scalar(statement)
        if refresh and row is not None:
            await self._db.refresh(row)
        return row

    async def _matches_request(
        self,
        row: McpCall,
        *,
        user_id: str,
        task_id: str,
        plan_step_id: str,
        internal_tool_name: str,
        arguments_digest: str,
    ) -> bool:
        task_user_id = await self._db.scalar(
            select(AnalysisTask.user_id).where(AnalysisTask.id == row.task_id)
        )
        return (
            row.task_id == task_id
            and row.plan_step_id == plan_step_id
            and row.internal_tool_name == internal_tool_name
            and row.arguments_digest == arguments_digest
            and task_user_id == user_id
        )
