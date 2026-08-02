"""Agent MCP 工具桥与 Agent 归属计费（设计文档 §11「MCP 故障与积分」）。

:class:`AgentMcpTool` 是 MCP 目录工具的 TrustedTool 执行器：
- 参数先按工具 Schema 校验归一化，再 canonical JSON + SHA-256 得
  ``arguments_hash``（§11.2 熔断键 / §8.1 agent_tool_calls）；
- **外发前持久化**：``agent_tool_calls`` 行（logical_call_id、状态）先落库并
  预留积分，再外发调用（§17.2「工具调用持久化先于外发」）；
- ``logical_call_id`` 由 run+step+工具+参数哈希确定性派生：相同调用重入只
  复用行，绝不重复执行或扣费；
- 四种故障分类（§11.1）：definitely_not_sent 释放、failed_confirmed 释放、
  result_unknown 保持预留进入恢复核对、settled 结算 10 积分；
- 成功时经 :class:`EvidenceWriter` 落不可变 Evidence（完整 raw_payload +
  受限 preview），模型只拿 evidence_id 与预览。

:class:`AgentMcpAccounting` 与 legacy ``McpAccounting`` 解耦：共用
``WalletService`` 的 reserve/settle/release，但挂靠 ``agent_tool_calls``，
完全不依赖 ``analysis_tasks``。幂等键固定为
``agent-mcp:{logical_call_id}:{reserve|settle|release}``。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.circuit_breaker import FineGrainedCircuitBreaker
from app.agent_runtime.evidence import EvidenceWriter
from app.agent_runtime.models import (
    AgentRun,
    AgentToolCall,
    AgentToolCallReconciliation,
)
from app.agent_runtime.tools.contracts import ToolContext, ToolResult
from app.billing.service import InsufficientPointsError, WalletService
from app.mcp_gateway.contracts import DataTapService
from app.mcp_gateway.registry import close_input_schema
from app.mcp_gateway.service import safe_upstream_text
from app.mcp_gateway.transport import (
    McpCircuitOpen,
    McpConnectionError,
    McpConnectionTimeout,
    McpGatewayTimeout,
    McpProtocolError,
    McpQueueTimeout,
    McpTransport,
    McpUpstreamHttpError,
    PossiblySentTimeout,
)
from app.mcp_gateway.validation import (
    McpValidationError,
    canonical_json_bytes,
    validate_input,
    validate_output,
)

# §11.3：每次 DataTap MCP 调用固定 10 积分。
MCP_POINTS_COST = 10

# §11.1 三种故障分类（settled 为 success，不在此枚举）。
DEFINITELY_NOT_SENT = "definitely_not_sent"
FAILED_CONFIRMED = "failed_confirmed"
RESULT_UNKNOWN = "result_unknown"

# 需要重新外发的 transport 异常 → result_unknown（请求可能已发出）。
_UNCONFIRMED_ERRORS = (
    PossiblySentTimeout,
    McpGatewayTimeout,
    McpUpstreamHttpError,
    McpProtocolError,
)
# 外发前即可确认未发出的异常 → definitely_not_sent。
_PRE_CONNECTION_ERRORS = (
    McpConnectionTimeout,
    McpConnectionError,
    McpQueueTimeout,
    McpCircuitOpen,
)

# Evidence scope/period 提取用到的参数键。
_SCOPE_KEYS = frozenset({"scope", "brand", "keyword", "platform", "datasource", "tags"})
_PERIOD_KEYS = frozenset(
    {"period", "start", "end", "start_date", "end_date", "date", "cycle_type"}
)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def arguments_hash(normalized_arguments: Mapping[str, Any]) -> str:
    """参数先按工具 Schema 归一化，再 canonical JSON + SHA-256。"""
    return hashlib.sha256(canonical_json_bytes(normalized_arguments)).hexdigest()


def logical_call_id_for(
    run_id: str, step_id: str, internal_tool_name: str, arguments_hash: str
) -> str:
    """确定性派生全局唯一 logical_call_id（§8.1）。"""
    raw = "\x00".join((run_id, step_id, internal_tool_name, arguments_hash))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@asynccontextmanager
async def _db_transaction(db: AsyncSession):
    """在既有事务内开 savepoint，否则开新事务；退出即提交。"""
    if db.in_transaction():
        async with db.begin_nested():
            yield
    else:
        async with db.begin():
            yield


def _extract_scope_period(arguments: Mapping[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    scope = {key: value for key, value in arguments.items() if key in _SCOPE_KEYS}
    period = {key: value for key, value in arguments.items() if key in _PERIOD_KEYS}
    return (scope if scope else None), (period if period else None)


class AgentMcpAccounting:
    """Agent 归属的 MCP 计费：挂靠 agent_tool_calls，不依赖 analysis_tasks。"""

    MCP_COST = MCP_POINTS_COST

    def __init__(self, db_session: AsyncSession) -> None:
        self._db = db_session
        self._wallets = WalletService(db_session)

    async def reserve(self, user_id: str, call: AgentToolCall) -> None:
        await self._wallets.reserve(
            user_id,
            self.MCP_COST,
            f"agent-mcp:{call.logical_call_id}:reserve",
            call.id,
            reference_type="agent_tool_call",
        )
        call.points_reserved = self.MCP_COST
        call.status = "reserved"
        await self._db.flush()

    async def mark_running(self, call: AgentToolCall) -> None:
        call.status = "running"
        call.started_at = call.started_at or _now()
        await self._db.flush()

    async def settle(self, user_id: str, call: AgentToolCall) -> None:
        await self._wallets.settle(
            user_id,
            self.MCP_COST,
            f"agent-mcp:{call.logical_call_id}:settle",
            call.id,
            reference_type="agent_tool_call",
        )
        call.points_settled = self.MCP_COST
        call.points_reserved = 0
        call.status = "settled"
        call.completed_at = _now()
        await self._db.flush()

    async def release(
        self,
        user_id: str,
        call: AgentToolCall,
        *,
        error_type: str | None,
        message: str | None,
    ) -> None:
        await self._wallets.release(
            user_id,
            self.MCP_COST,
            f"agent-mcp:{call.logical_call_id}:release",
            call.id,
            reference_type="agent_tool_call",
        )
        call.points_reserved = 0
        call.points_settled = 0
        call.status = "failed"
        call.error_type = error_type
        call.safe_error_message = message
        call.completed_at = _now()
        await self._db.flush()


class AgentMcpTool:
    """已审核 DataTap MCP 目录工具的受控执行器（TrustedTool 兼容）。"""

    name: str
    input_model: type[BaseModel] | None = None
    points_cost: int = MCP_POINTS_COST
    external_side_effect: bool = True

    def __init__(
        self,
        *,
        internal_name: str,
        service: DataTapService,
        remote_name: str,
        input_schema: dict[str, Any],
        output_schema: dict[str, Any],
        db_session: AsyncSession,
        transport: McpTransport,
        breaker: FineGrainedCircuitBreaker | None = None,
    ) -> None:
        if not isinstance(service, DataTapService):
            raise TypeError("service must be a DataTapService")
        self.name = internal_name
        self._service = service
        self._remote_name = remote_name
        self._input_schema = close_input_schema(input_schema)
        self._output_schema = output_schema
        self._db = db_session
        self._transport = transport
        self._breaker = breaker or FineGrainedCircuitBreaker()
        self._accounting = AgentMcpAccounting(db_session)
        self._evidence = EvidenceWriter(db_session)

    # ------------------------------------------------------------------ #
    # execute
    # ------------------------------------------------------------------ #

    async def execute(
        self, context: ToolContext, arguments: BaseModel | Mapping[str, Any]
    ) -> ToolResult:
        if context.step_id is None:
            raise ValueError("step_id is required for MCP tool calls")
        raw = dict(arguments.model_dump()) if isinstance(arguments, BaseModel) else dict(arguments)
        try:
            normalized = validate_input(raw, self._input_schema)
        except McpValidationError:
            return ToolResult(
                status="failed",
                safe_summary="tool arguments failed schema validation",
                error_type=DEFINITELY_NOT_SENT,
            )

        args_hash = arguments_hash(normalized)
        logical_call_id = logical_call_id_for(
            context.run_id, context.step_id, self.name, args_hash
        )
        existing = await self._by_logical_call_id(logical_call_id)
        if existing is not None:
            return await self._replay(existing)

        # 细粒度熔断：只封锁相同 service+tool+参数（§11.2）。
        if not self._breaker.allow(
            service=self._service.value, internal_tool_name=self.name, arguments=normalized
        ):
            return ToolResult(
                status="failed",
                safe_summary="circuit open for this exact tool call",
                error_type=DEFINITELY_NOT_SENT,
            )

        row = AgentToolCall(
            id=str(uuid4()),
            run_id=context.run_id,
            step_id=context.step_id,
            logical_call_id=logical_call_id,
            service=self._service.value,
            internal_tool_name=self.name,
            arguments_json=normalized,
            arguments_hash=args_hash,
            status="planned",
        )
        self._db.add(row)
        # 外发前持久化：行 + 预留原子落库；余额不足不落预留。
        try:
            async with _db_transaction(self._db):
                await self._accounting.reserve(context.user_id, row)
        except InsufficientPointsError:
            return ToolResult(
                status="failed",
                safe_summary="insufficient points for MCP call",
                error_type=DEFINITELY_NOT_SENT,
            )
        async with _db_transaction(self._db):
            await self._accounting.mark_running(row)

        try:
            result = await self._transport.call_tool(
                self._service, self._remote_name, normalized
            )
        except _UNCONFIRMED_ERRORS as exc:
            return await self._finalize_unknown(row, normalized, message=self._error_message(exc))
        except _PRE_CONNECTION_ERRORS as exc:
            return await self._finalize_definitely_not_sent(
                row, context, message=self._error_message(exc)
            )
        except Exception as exc:
            return await self._finalize_unknown(row, normalized, message=self._error_message(exc))

        if result.is_error:
            self._breaker.record_success(
                service=self._service.value, internal_tool_name=self.name, arguments=normalized
            )
            async with _db_transaction(self._db):
                row.upstream_request_id = result.upstream_request_id
                await self._accounting.release(
                    context.user_id,
                    row,
                    error_type=FAILED_CONFIRMED,
                    message=safe_upstream_text(result.error_text) or "MCP call failed",
                )
            return ToolResult(
                status="failed", safe_summary="upstream reported a business error",
                error_type=FAILED_CONFIRMED,
            )

        if result.structured_content is not None:
            try:
                validated = validate_output(result.structured_content, self._output_schema)
            except McpValidationError:
                self._breaker.record_success(
                    service=self._service.value, internal_tool_name=self.name, arguments=normalized
                )
                async with _db_transaction(self._db):
                    row.upstream_request_id = result.upstream_request_id
                    await self._accounting.release(
                        context.user_id, row, error_type=FAILED_CONFIRMED,
                        message="output validation failed",
                    )
                return ToolResult(
                    status="failed", safe_summary="output validation failed",
                    error_type=FAILED_CONFIRMED,
                )
        else:
            validated = None

        self._breaker.record_success(
            service=self._service.value, internal_tool_name=self.name, arguments=normalized
        )
        scope, period = _extract_scope_period(normalized)
        async with _db_transaction(self._db):
            evidence = await self._evidence.write(
                session_id=context.session_id,
                run_id=context.run_id,
                tool_call_id=row.id,
                source_type="mcp",
                source_name=self.name,
                scope_json=scope,
                period_json=period,
                raw_payload=validated,
            )
            row.upstream_request_id = result.upstream_request_id
            await self._accounting.settle(context.user_id, row)
        summary = json.dumps(evidence.normalized_preview_json, ensure_ascii=False)
        return ToolResult(
            status="success",
            safe_summary=summary[:1_000],
            evidence_id=evidence.id,
        )

    # ------------------------------------------------------------------ #
    # 恢复核对：按 logical_call_id 只读核对、绝不重放（§11.1）
    # ------------------------------------------------------------------ #

    async def reconcile(self, logical_call_id: str) -> ToolResult:
        """恢复任务入口：对 result_unknown 调用按 logical_call_id 只读核对。"""
        row = await self._by_logical_call_id(logical_call_id)
        if row is None:
            raise LookupError("agent_tool_call_not_found")
        if row.status not in ("unknown", "reserved"):
            return await self._replay(row)
        user_id = await self._user_id(row)
        if row.upstream_request_id is None:
            await self._append_reconciliation(
                row, source="upstream_probe", decision="keep_unknown",
                note="no upstream_request_id to reconcile",
            )
            return ToolResult(
                status="unknown", safe_summary="cannot reconcile without upstream_request_id",
                error_type=RESULT_UNKNOWN,
            )
        recon = getattr(self._transport, "reconcile_tool_call", None)
        if recon is None:
            await self._append_reconciliation(
                row, source="upstream_probe", decision="keep_unknown",
                note="transport does not support reconciliation",
            )
            return ToolResult(
                status="unknown", safe_summary="transport cannot reconcile",
                error_type=RESULT_UNKNOWN,
            )
        result = await recon(row.upstream_request_id)
        if result is None:
            await self._append_reconciliation(
                row, source="upstream_probe", decision="keep_unknown",
                note="outcome not confirmable",
            )
            return ToolResult(
                status="unknown", safe_summary="outcome not confirmable",
                error_type=RESULT_UNKNOWN,
            )
        if result.is_error:
            async with _db_transaction(self._db):
                await self._accounting.release(
                    user_id, row, error_type=FAILED_CONFIRMED,
                    message="upstream confirmed failure",
                )
            await self._append_reconciliation(
                row, source="upstream_probe", decision="confirm_failure",
                note="upstream reported isError",
            )
            return ToolResult(
                status="failed", safe_summary="upstream confirmed failure",
                error_type=FAILED_CONFIRMED,
            )
        # 可确认成功；能取回 payload 则落 Evidence，否则只结算并标记结果不可用。
        if result.structured_content is None:
            async with _db_transaction(self._db):
                await self._accounting.settle(user_id, row)
                row.safe_error_message = "result_unavailable"
            await self._append_reconciliation(
                row, source="upstream_probe", decision="confirm_success",
                note="payload not retrievable",
            )
            return ToolResult(
                status="success", safe_summary="confirmed success (payload unavailable)",
                evidence_id=None,
            )
        scope, period = _extract_scope_period(row.arguments_json or {})
        async with _db_transaction(self._db):
            evidence = await self._evidence.write(
                session_id=(await self._run(row)).session_id,
                run_id=row.run_id,
                tool_call_id=row.id,
                source_type="mcp",
                source_name=self.name,
                scope_json=scope,
                period_json=period,
                raw_payload=result.structured_content,
            )
            row.upstream_request_id = result.upstream_request_id or row.upstream_request_id
            await self._accounting.settle(user_id, row)
        await self._append_reconciliation(
            row, source="upstream_probe", decision="confirm_success",
            note="confirmed via upstream",
        )
        return ToolResult(status="success", safe_summary="confirmed success", evidence_id=evidence.id)

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #

    async def _replay(self, row: AgentToolCall) -> ToolResult:
        if row.status == "settled":
            evidence = await self._evidence.get_by_tool_call_id(row.id)
            if evidence is not None:
                return ToolResult(status="success", safe_summary="already settled", evidence_id=evidence.id)
            return ToolResult(status="success", safe_summary="already settled")
        if row.status == "failed":
            return ToolResult(
                status="failed",
                safe_summary=row.safe_error_message or "tool call failed",
                error_type=row.error_type or FAILED_CONFIRMED,
            )
        if row.status == "unknown":
            return ToolResult(
                status="unknown",
                safe_summary=row.safe_error_message or "result unknown",
                error_type=RESULT_UNKNOWN,
            )
        return ToolResult(
            status="unknown",
            safe_summary="tool call already in progress; awaiting recovery",
            error_type=RESULT_UNKNOWN,
        )

    async def _finalize_unknown(self, row: AgentToolCall, normalized, *, message: str) -> ToolResult:
        self._breaker.record_failure(
            service=self._service.value, internal_tool_name=self.name, arguments=normalized
        )
        async with _db_transaction(self._db):
            row.status = "unknown"
            row.error_type = RESULT_UNKNOWN
            row.safe_error_message = message
            row.completed_at = _now()
        return ToolResult(status="unknown", safe_summary=message, error_type=RESULT_UNKNOWN)

    async def _finalize_definitely_not_sent(
        self, row: AgentToolCall, context: ToolContext, *, message: str
    ) -> ToolResult:
        async with _db_transaction(self._db):
            await self._accounting.release(
                context.user_id, row, error_type=DEFINITELY_NOT_SENT, message=message
            )
        return ToolResult(status="failed", safe_summary=message, error_type=DEFINITELY_NOT_SENT)

    @staticmethod
    def _error_message(exc: BaseException) -> str:
        return str(exc) or exc.__class__.__name__

    async def _by_logical_call_id(self, logical_call_id: str) -> AgentToolCall | None:
        return await self._db.scalar(
            select(AgentToolCall).where(AgentToolCall.logical_call_id == logical_call_id)
        )

    async def _run(self, call: AgentToolCall) -> AgentRun:
        run = await self._db.get(AgentRun, call.run_id)
        if run is None:
            raise LookupError("agent_run_not_found")
        return run

    async def _user_id(self, call: AgentToolCall) -> str:
        return (await self._run(call)).user_id

    async def _append_reconciliation(
        self,
        call: AgentToolCall,
        *,
        source: str,
        decision: str,
        note: str,
        actor_user_id: str | None = None,
    ) -> None:
        self._db.add(
            AgentToolCallReconciliation(
                id=str(uuid4()),
                tool_call_id=call.id,
                source=source,
                decision=decision,
                actor_user_id=actor_user_id,
                note=note,
                created_at=_now(),
            )
        )
        await self._db.flush()


__all__ = [
    "DEFINITELY_NOT_SENT",
    "FAILED_CONFIRMED",
    "MCP_POINTS_COST",
    "RESULT_UNKNOWN",
    "AgentMcpAccounting",
    "AgentMcpTool",
    "arguments_hash",
    "logical_call_id_for",
]
