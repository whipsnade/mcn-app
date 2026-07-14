from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp_gateway.contracts import DataTapService, McpCallStatus
from app.mcp_gateway.models import McpCall
from app.mcp_gateway.registry import ToolRegistryService
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


class McpCallService:
    def __init__(
        self,
        db_session: AsyncSession,
        transport: McpTransport,
        *,
        registry: ToolRegistryService | None = None,
    ) -> None:
        self._db = db_session
        self._transport = transport
        self._registry = registry or ToolRegistryService(db_session, transport)

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
            task_user_id = await self._db.scalar(
                select(AnalysisTask.user_id).where(AnalysisTask.id == existing.task_id)
            )
            if (
                existing.task_id != task_id
                or existing.plan_step_id != plan_step_id
                or existing.internal_tool_name != internal_tool_name
                or existing.arguments_digest != arguments_digest
                or task_user_id != user_id
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
        validated_arguments = validate_input(arguments, approved.input_schema)
        pending_invocation = json.loads(
            canonical_json_bytes(
                {
                    "remote_name": approved.remote_name,
                    "pending_arguments": validated_arguments,
                    "input_schema": approved.input_schema,
                    "output_schema": approved.output_schema,
                }
            )
        )
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
            evidence_json={"pending_invocation": pending_invocation},
            created_at=now,
            updated_at=now,
        )
        self._db.add(row)
        await self._db.flush()
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
        if row is None:
            raise LookupError("MCP logical call does not exist")
        if claimed.rowcount != 1:
            return row

        pending = row.evidence_json.get("pending_invocation") if row.evidence_json else None
        if not isinstance(pending, dict):
            return await self._finish_failed(row, "pending_invocation_missing")
        remote_name = pending.get("remote_name")
        arguments = pending.get("pending_arguments")
        input_schema = pending.get("input_schema")
        output_schema = pending.get("output_schema")
        if (
            not isinstance(remote_name, str)
            or not isinstance(arguments, dict)
            or not isinstance(input_schema, dict)
            or not isinstance(output_schema, dict)
        ):
            return await self._finish_failed(row, "pending_invocation_invalid")
        try:
            validated_arguments = validate_input(arguments, input_schema)
        except McpValidationError:
            return await self._finish_failed(row, "input_validation_error")

        try:
            result = await self._transport.call_tool(
                DataTapService(row.service_slug), remote_name, validated_arguments
            )
        except PossiblySentTimeout:
            return await self._finish_unknown(row)
        except Exception:
            return await self._finish_failed(row, "upstream_error")

        if result.is_error:
            row.upstream_request_id = result.upstream_request_id
            return await self._finish_failed(row, "upstream_tool_error")
        try:
            validated_output = validate_output(result.structured_content, output_schema)
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
        await self._db.flush()
        return row

    async def _finish_unknown(self, row: McpCall) -> McpCall:
        row.status = McpCallStatus.UNKNOWN.value
        row.evidence_json = {"outcome": "unknown"}
        row.error_type = "possibly_sent_timeout"
        row.error_message = "MCP outcome could not be confirmed"
        row.completed_at = datetime.now(UTC).replace(tzinfo=None)
        row.updated_at = row.completed_at
        await self._db.flush()
        return row

    async def _finish_failed(self, row: McpCall, error_type: str) -> McpCall:
        row.status = McpCallStatus.FAILED.value
        row.evidence_json = {"outcome": "failed"}
        row.error_type = error_type
        row.error_message = "MCP call failed"
        row.completed_at = datetime.now(UTC).replace(tzinfo=None)
        row.updated_at = row.completed_at
        await self._db.flush()
        return row

    async def _by_logical_id(
        self, logical_call_id: str, *, refresh: bool = False
    ) -> McpCall | None:
        row = await self._db.scalar(
            select(McpCall).where(McpCall.logical_call_id == logical_call_id)
        )
        if refresh and row is not None:
            await self._db.refresh(row)
        return row
