"""Agent MCP 工具桥与 Agent 归属计费（设计文档 §11「MCP 故障与积分」/ 加固 §5.3）。

:class:`AgentMcpTool` 是 MCP 目录工具的 TrustedTool 执行器；所有数据库读写经
:class:`DurableToolCallCoordinator` 的**独立会话、独立事务**：

- ``prepare`` 单一事务完成 Run/Step 归属校验、``logical_call_id`` 计算与锁定、
  ``agent_tool_calls`` 行插入或复用、10 积分预留、``running`` + ``started_at``
  落库——**commit 之后才允许调用 transport**（durable-before-send，§2.1/§5.3）。
  预留与调用行在同一事务，不存在"悬挂预留"窗口；
- 外发后 finalize 各自独立事务：成功 = 输出 Schema 校验 + 写 Evidence + settle；
  ``failed_confirmed`` / ``definitely_not_sent`` = release；``result_unknown`` =
  保留预留置 unknown 进入恢复核对；进程取消且请求可能已发送 = 按
  ``result_unknown`` 收口；
- ``logical_call_id`` 由 run+step+工具+参数哈希确定性派生：相同调用重入只
  复用已提交行（幂等回放），绝不重复执行或扣费；
- 恢复核对（reconcile）同样走独立事务并即时提交；取回的 payload 必须重新过
  输出 Schema 校验才能写 Evidence（与 execute 路径一致，§5.3）。

:class:`AgentMcpAccounting` 与 legacy ``McpAccounting`` 解耦：共用
``WalletService`` 的 reserve/settle/release，但挂靠 ``agent_tool_calls``，
完全不依赖 ``analysis_tasks``。幂等键固定为
``agent-mcp:{logical_call_id}:{reserve|settle|release}``。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.circuit_breaker import FineGrainedCircuitBreaker
from app.agent_runtime.evidence import EvidenceWriter
from app.agent_runtime.normalization import NormalizationRegistry
from app.agent_runtime.models import (
    AgentRun,
    AgentStep,
    AgentToolCall,
    AgentToolCallReconciliation,
)
from app.agent_runtime.tools.contracts import (
    ToolContext,
    ToolResult,
    arguments_hash,
    logical_call_id_for,
)
from app.billing.service import InsufficientPointsError, WalletService
from app.db.session import SessionFactory
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

# 会话工厂类型：生产为 SessionFactory（独立连接真实提交）；测试可注入共享会话。
SessionFactoryLike = Callable[[], AbstractAsyncContextManager[AsyncSession]]


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _extract_scope_period(arguments: Mapping[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    scope = {key: value for key, value in arguments.items() if key in _SCOPE_KEYS}
    period = {key: value for key, value in arguments.items() if key in _PERIOD_KEYS}
    return (scope if scope else None), (period if period else None)


@dataclass(frozen=True)
class AgentToolCallSnapshot:
    """协调器读模型：reconcile 决策所需的调用行 + 归属信息（脱离会话的纯数据）。"""

    call_id: str
    logical_call_id: str
    run_id: str
    step_id: str
    user_id: str
    session_id: str
    status: str
    error_type: str | None
    safe_error_message: str | None
    upstream_request_id: str | None
    arguments_json: dict[str, Any] | None


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


class DurableToolCallCoordinator:
    """MCP 调用的事务边界（§5.3）：外发前持久化、外发后结算、恢复核对。

    每个操作使用**独立 DB Session 的单一事务并即时提交**：

    - :meth:`prepare` 提交后调用行（running + 10 分预留）对其它连接已可见，
      之后才允许外发——进程在"外发后、返回前"崩溃时行与预留不再整体回滚；
    - finalize 三个方法各自独立提交，终态守护 + 钱包幂等键保证重入安全；
    - reconcile 系列方法（load/keep_unknown/confirm_*）同样即时提交，
      恢复循环与人工核对的结果不再依赖外层会话的提交时机。
    """

    def __init__(
        self,
        *,
        session_factory: SessionFactoryLike,
        service: DataTapService,
        internal_tool_name: str,
    ) -> None:
        self._session_factory = session_factory
        self._service = service
        self._internal_tool_name = internal_tool_name

    # ------------------------------------------------------------------ #
    # prepare：外发前持久化（§5.3）
    # ------------------------------------------------------------------ #

    async def prepare(
        self,
        context: ToolContext,
        *,
        logical_call_id: str,
        args_hash: str,
        normalized_arguments: Mapping[str, Any],
    ) -> ToolResult | None:
        """独立会话单一事务：校验归属 → 锁定 logical_call_id → 插入或复用行 →
        预留 10 积分 → 写 running + started_at → **commit**。

        返回 ``None`` 表示行已提交 running、允许外发；返回 :class:`ToolResult`
        表示幂等回放（既有行 / 余额不足），调用方不得外发。
        """
        if context.step_id is None:
            raise ValueError("step_id is required for MCP tool calls")
        async with self._session_factory() as db:
            try:
                # 1. 校验 Run/Step 归属（模型不能伪造他人上下文）。
                run = await db.get(AgentRun, context.run_id)
                if (
                    run is None
                    or run.user_id != context.user_id
                    or run.session_id != context.session_id
                ):
                    raise ValueError("agent_run_context_mismatch")
                step = await db.get(AgentStep, context.step_id)
                if step is None or step.run_id != run.id:
                    raise ValueError("agent_step_context_mismatch")
                # 2. 计算并锁定 logical_call_id：已存在则幂等回放（绝不重发）。
                existing = await self._by_logical_call_id(db, logical_call_id, for_update=True)
                if existing is not None:
                    return await self._replay(db, existing)
                # 3-5. 行 + 预留 + running 同一事务提交。
                row = AgentToolCall(
                    id=str(uuid4()),
                    run_id=context.run_id,
                    step_id=context.step_id,
                    logical_call_id=logical_call_id,
                    service=self._service.value,
                    internal_tool_name=self._internal_tool_name,
                    arguments_json=dict(normalized_arguments),
                    arguments_hash=args_hash,
                    status="planned",
                )
                db.add(row)
                accounting = AgentMcpAccounting(db)
                await accounting.reserve(context.user_id, row)
                await accounting.mark_running(row)
                await db.commit()
                return None
            except InsufficientPointsError:
                # 行与预留同一事务整体回滚：不残留 planned 行、不残留预留。
                await db.rollback()
                return ToolResult(
                    status="failed",
                    safe_summary="insufficient points for MCP call",
                    error_type=DEFINITELY_NOT_SENT,
                )
            except IntegrityError:
                # 并发窗口 TOCTOU：另一调用先提交了相同 logical_call_id（§17.2）。
                # 唯一约束失败视为已存在 → 幂等回放其行，绝不重发。
                await db.rollback()
                winner = await self._by_logical_call_id(db, logical_call_id, for_update=True)
                if winner is not None:
                    return await self._replay(db, winner)
                raise

    # ------------------------------------------------------------------ #
    # finalize：外发后结算（各自独立事务，§5.3）
    # ------------------------------------------------------------------ #

    async def finalize_success(
        self,
        *,
        logical_call_id: str,
        user_id: str,
        session_id: str,
        validated_payload: Any,
        upstream_request_id: str | None,
    ) -> tuple[str, dict[str, Any] | None]:
        """成功收口：写 Evidence + settle 10 分，返回 (evidence_id, preview)。

        幂等：已 settled 的调用直接回放既有 Evidence（重入不重复写、不重复扣费）。
        """
        async with self._session_factory() as db:
            row = await self._require_call(db, logical_call_id, for_update=True)
            if row.status == "settled":
                evidence = await EvidenceWriter(db).get_by_tool_call_id(row.id)
                if evidence is None:
                    raise LookupError("evidence_missing_for_settled_call")
                return evidence.id, evidence.normalized_preview_json
            if row.status == "failed":
                raise RuntimeError("agent_tool_call_already_failed")
            scope, period = _extract_scope_period(row.arguments_json or {})
            evidence = await EvidenceWriter(db).write(
                session_id=session_id,
                run_id=row.run_id,
                tool_call_id=row.id,
                source_type="mcp",
                source_name=self._internal_tool_name,
                scope_json=scope,
                period_json=period,
                raw_payload=validated_payload,
                normalization=NormalizationRegistry().normalize(
                    self._internal_tool_name, validated_payload
                ),
            )
            row.upstream_request_id = upstream_request_id
            await AgentMcpAccounting(db).settle(user_id, row)
            await db.commit()
            return evidence.id, evidence.normalized_preview_json

    async def finalize_release(
        self,
        *,
        logical_call_id: str,
        user_id: str,
        error_type: str,
        message: str,
        upstream_request_id: str | None = None,
    ) -> None:
        """失败收口（definitely_not_sent / failed_confirmed）：释放预留。"""
        async with self._session_factory() as db:
            row = await self._require_call(db, logical_call_id, for_update=True)
            if row.status in ("settled", "failed"):
                return  # 已终态：幂等跳过，不重复触碰钱包
            if upstream_request_id:
                row.upstream_request_id = upstream_request_id
            await AgentMcpAccounting(db).release(
                user_id, row, error_type=error_type, message=message
            )
            await db.commit()

    async def finalize_unknown(
        self,
        *,
        logical_call_id: str,
        message: str,
        upstream_request_id: str | None = None,
    ) -> None:
        """result_unknown 收口：保留预留、置 unknown，进入恢复核对。"""
        async with self._session_factory() as db:
            row = await self._require_call(db, logical_call_id, for_update=True)
            if row.status in ("settled", "failed"):
                return  # 已终态：幂等跳过（与并发 finalize/核对竞态安全）
            if upstream_request_id:
                row.upstream_request_id = upstream_request_id
            row.status = "unknown"
            row.error_type = RESULT_UNKNOWN
            row.safe_error_message = message
            row.completed_at = _now()
            await db.commit()

    # ------------------------------------------------------------------ #
    # 恢复核对（reconcile）：独立事务即时提交，绝不重放（§11.1 / §5.3）
    # ------------------------------------------------------------------ #

    async def load_call(self, logical_call_id: str) -> AgentToolCallSnapshot | None:
        """独立会话读取调用行 + 归属快照（reconcile 决策的只读输入）。"""
        async with self._session_factory() as db:
            row = await self._by_logical_call_id(db, logical_call_id)
            if row is None:
                return None
            run = await db.get(AgentRun, row.run_id)
            if run is None:
                raise LookupError("agent_run_not_found")
            return AgentToolCallSnapshot(
                call_id=row.id,
                logical_call_id=row.logical_call_id,
                run_id=row.run_id,
                step_id=row.step_id,
                user_id=run.user_id,
                session_id=run.session_id,
                status=row.status,
                error_type=row.error_type,
                safe_error_message=row.safe_error_message,
                upstream_request_id=row.upstream_request_id,
                arguments_json=row.arguments_json,
            )

    async def replay_result(self, logical_call_id: str) -> ToolResult | None:
        """独立会话按当前状态构造幂等回放结果；行不存在返回 None。"""
        async with self._session_factory() as db:
            row = await self._by_logical_call_id(db, logical_call_id)
            if row is None:
                return None
            return await self._replay(db, row)

    async def keep_unknown(self, snapshot: AgentToolCallSnapshot, *, note: str) -> None:
        """追加 keep_unknown 审计；同一调用已记录过 keep_unknown 则跳过。

        无法核对的 unknown 调用每轮恢复扫描都会被再次探测，但不得每轮都追加一条
        审计（约 2880 行/天/调用）——状态未变化时不重复记账（§11.1）。
        """
        async with self._session_factory() as db:
            last = await self._last_reconciliation_decision(db, snapshot.call_id)
            if last == "keep_unknown":
                return
            self._append_reconciliation(
                db,
                snapshot.call_id,
                source="upstream_probe",
                decision="keep_unknown",
                note=note,
            )
            await db.commit()

    async def confirm_failure(
        self,
        snapshot: AgentToolCallSnapshot,
        *,
        message: str,
        note: str,
        upstream_request_id: str | None = None,
    ) -> None:
        """核对确认失败：release + confirm_failure 审计（独立事务提交）。"""
        async with self._session_factory() as db:
            row = await self._require_call(db, snapshot.logical_call_id, for_update=True)
            if row.status in ("settled", "failed"):
                return
            if upstream_request_id:
                row.upstream_request_id = upstream_request_id
            await AgentMcpAccounting(db).release(
                snapshot.user_id, row, error_type=FAILED_CONFIRMED, message=message
            )
            self._append_reconciliation(
                db,
                row.id,
                source="upstream_probe",
                decision="confirm_failure",
                note=note,
            )
            await db.commit()

    async def confirm_success(
        self,
        snapshot: AgentToolCallSnapshot,
        *,
        validated_payload: Any,
        upstream_request_id: str | None,
        note: str,
    ) -> str | None:
        """核对确认成功：（有 payload 时）写 Evidence + settle + 审计。

        ``validated_payload`` 为 ``None`` 表示 payload 不可取回：只结算并标记
        ``result_unavailable``，绝不伪造 Evidence。返回 evidence_id 或 None。
        """
        async with self._session_factory() as db:
            row = await self._require_call(db, snapshot.logical_call_id, for_update=True)
            if row.status in ("settled", "failed"):
                evidence = await EvidenceWriter(db).get_by_tool_call_id(row.id)
                return evidence.id if evidence is not None else None
            evidence_id: str | None = None
            if validated_payload is not None:
                scope, period = _extract_scope_period(row.arguments_json or {})
                evidence = await EvidenceWriter(db).write(
                    session_id=snapshot.session_id,
                    run_id=row.run_id,
                    tool_call_id=row.id,
                    source_type="mcp",
                    source_name=self._internal_tool_name,
                    scope_json=scope,
                    period_json=period,
                    raw_payload=validated_payload,
                    normalization=NormalizationRegistry().normalize(
                        self._internal_tool_name, validated_payload
                    ),
                )
                evidence_id = evidence.id
            if upstream_request_id:
                row.upstream_request_id = upstream_request_id
            await AgentMcpAccounting(db).settle(snapshot.user_id, row)
            if evidence_id is None:
                row.safe_error_message = "result_unavailable"
            self._append_reconciliation(
                db,
                row.id,
                source="upstream_probe",
                decision="confirm_success",
                note=note,
            )
            await db.commit()
            return evidence_id

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #

    async def _replay(self, db: AsyncSession, row: AgentToolCall) -> ToolResult:
        if row.status == "settled":
            evidence = await EvidenceWriter(db).get_by_tool_call_id(row.id)
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

    async def _by_logical_call_id(
        self, db: AsyncSession, logical_call_id: str, *, for_update: bool = False
    ) -> AgentToolCall | None:
        statement = select(AgentToolCall).where(
            AgentToolCall.logical_call_id == logical_call_id
        )
        if for_update:
            statement = statement.with_for_update()
        return await db.scalar(statement)

    async def _require_call(
        self, db: AsyncSession, logical_call_id: str, *, for_update: bool = False
    ) -> AgentToolCall:
        row = await self._by_logical_call_id(db, logical_call_id, for_update=for_update)
        if row is None:
            raise LookupError("agent_tool_call_not_found")
        return row

    def _append_reconciliation(
        self,
        db: AsyncSession,
        tool_call_id: str,
        *,
        source: str,
        decision: str,
        note: str,
    ) -> None:
        db.add(
            AgentToolCallReconciliation(
                id=str(uuid4()),
                tool_call_id=tool_call_id,
                source=source,
                decision=decision,
                note=note,
                created_at=_now(),
            )
        )

    async def _last_reconciliation_decision(
        self, db: AsyncSession, tool_call_id: str
    ) -> str | None:
        return await db.scalar(
            select(AgentToolCallReconciliation.decision)
            .where(AgentToolCallReconciliation.tool_call_id == tool_call_id)
            .order_by(
                AgentToolCallReconciliation.created_at.desc(),
                AgentToolCallReconciliation.id.desc(),
            )
            .limit(1)
        )


class AgentMcpTool:
    """已审核 DataTap MCP 目录工具的受控执行器（TrustedTool 兼容）。

    ``db_session`` 为构造兼容参数（A2 装配路径按 Engine 会话传入）；所有
    durable 读写经 ``session_factory`` 独立会话提交，不再使用该会话。
    """

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
        transport: McpTransport,
        db_session: AsyncSession | None = None,
        breaker: FineGrainedCircuitBreaker | None = None,
        session_factory: SessionFactoryLike = SessionFactory,
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
        self._coordinator = DurableToolCallCoordinator(
            session_factory=session_factory,
            service=service,
            internal_tool_name=internal_name,
        )

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

        # 细粒度熔断：只封锁相同 service+tool+参数（§11.2）。
        if not self._breaker.allow(
            service=self._service.value, internal_tool_name=self.name, arguments=normalized
        ):
            return ToolResult(
                status="failed",
                safe_summary="circuit open for this exact tool call",
                error_type=DEFINITELY_NOT_SENT,
            )

        # durable-before-send：行 + 预留 commit 之后才允许外发（§2.1/§5.3）。
        replay = await self._coordinator.prepare(
            context,
            logical_call_id=logical_call_id,
            args_hash=args_hash,
            normalized_arguments=normalized,
        )
        if replay is not None:
            return replay

        try:
            result = await self._transport.call_tool(
                self._service, self._remote_name, normalized
            )
        except asyncio.CancelledError:
            # 进程取消且请求可能已发送：按 result_unknown 收口（§5.3），
            # 绝不静默丢失已提交行与预留；随后继续抛出取消。
            self._breaker.record_failure(
                service=self._service.value, internal_tool_name=self.name, arguments=normalized
            )
            try:
                await self._coordinator.finalize_unknown(
                    logical_call_id=logical_call_id,
                    message="cancelled after dispatch; outcome unconfirmed",
                )
            finally:
                raise
        except _UNCONFIRMED_ERRORS as exc:
            return await self._finalize_unknown(
                logical_call_id, normalized, message=self._error_message(exc)
            )
        except _PRE_CONNECTION_ERRORS as exc:
            return await self._finalize_definitely_not_sent(
                context, logical_call_id, normalized, message=self._error_message(exc)
            )
        except Exception as exc:
            return await self._finalize_unknown(
                logical_call_id, normalized, message=self._error_message(exc)
            )

        if result.is_error:
            self._breaker.record_success(
                service=self._service.value, internal_tool_name=self.name, arguments=normalized
            )
            upstream_message = safe_upstream_text(result.error_text)
            await self._coordinator.finalize_release(
                logical_call_id=logical_call_id,
                user_id=context.user_id,
                error_type=FAILED_CONFIRMED,
                message=upstream_message or "MCP call failed",
                upstream_request_id=result.upstream_request_id,
            )
            # 上游业务错误文本（已脱敏截断）必须回喂模型——"不支持的维度/
            # 不支持的平台"这类信息是模型修正参数的唯一依据，只存库不回喂
            # 会导致模型用同样错误参数反复调用（真实 UAT 已观察到该模式）。
            return ToolResult(
                status="failed",
                safe_summary=upstream_message or "upstream reported a business error",
                error_type=FAILED_CONFIRMED,
            )

        if result.structured_content is not None:
            try:
                validated = validate_output(result.structured_content, self._output_schema)
            except McpValidationError:
                self._breaker.record_success(
                    service=self._service.value, internal_tool_name=self.name, arguments=normalized
                )
                await self._coordinator.finalize_release(
                    logical_call_id=logical_call_id,
                    user_id=context.user_id,
                    error_type=FAILED_CONFIRMED,
                    message="output validation failed",
                    upstream_request_id=result.upstream_request_id,
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
        evidence_id, preview = await self._coordinator.finalize_success(
            logical_call_id=logical_call_id,
            user_id=context.user_id,
            session_id=context.session_id,
            validated_payload=validated,
            upstream_request_id=result.upstream_request_id,
        )
        summary = json.dumps(preview, ensure_ascii=False)
        return ToolResult(
            status="success",
            safe_summary=summary[:1_000],
            evidence_id=evidence_id,
        )

    # ------------------------------------------------------------------ #
    # 恢复核对：按 logical_call_id 只读核对、绝不重放（§11.1）
    # ------------------------------------------------------------------ #

    async def reconcile(self, logical_call_id: str) -> ToolResult:
        """恢复任务入口：对 result_unknown 调用按 logical_call_id 只读核对。"""
        snapshot = await self._coordinator.load_call(logical_call_id)
        if snapshot is None:
            raise LookupError("agent_tool_call_not_found")
        if snapshot.status not in ("unknown", "reserved"):
            replay = await self._coordinator.replay_result(logical_call_id)
            assert replay is not None  # load_call 刚读到该行
            return replay
        if snapshot.upstream_request_id is None:
            await self._coordinator.keep_unknown(
                snapshot, note="no upstream_request_id to reconcile"
            )
            return ToolResult(
                status="unknown", safe_summary="cannot reconcile without upstream_request_id",
                error_type=RESULT_UNKNOWN,
            )
        recon = getattr(self._transport, "reconcile_tool_call", None)
        if recon is None:
            await self._coordinator.keep_unknown(
                snapshot, note="transport does not support reconciliation"
            )
            return ToolResult(
                status="unknown", safe_summary="transport cannot reconcile",
                error_type=RESULT_UNKNOWN,
            )
        result = await recon(snapshot.upstream_request_id)
        if result is None:
            await self._coordinator.keep_unknown(snapshot, note="outcome not confirmable")
            return ToolResult(
                status="unknown", safe_summary="outcome not confirmable",
                error_type=RESULT_UNKNOWN,
            )
        if result.is_error:
            await self._coordinator.confirm_failure(
                snapshot,
                message="upstream confirmed failure",
                note="upstream reported isError",
                upstream_request_id=result.upstream_request_id,
            )
            return ToolResult(
                status="failed", safe_summary="upstream confirmed failure",
                error_type=FAILED_CONFIRMED,
            )
        # 可确认成功；payload 必须重新过输出 Schema 校验才能写 Evidence（§5.3）。
        if result.structured_content is None:
            await self._coordinator.confirm_success(
                snapshot,
                validated_payload=None,
                upstream_request_id=result.upstream_request_id,
                note="payload not retrievable",
            )
            return ToolResult(
                status="success", safe_summary="confirmed success (payload unavailable)",
                evidence_id=None,
            )
        try:
            validated = validate_output(result.structured_content, self._output_schema)
        except McpValidationError:
            await self._coordinator.confirm_failure(
                snapshot,
                message="output validation failed",
                note="output validation failed",
                upstream_request_id=result.upstream_request_id,
            )
            return ToolResult(
                status="failed", safe_summary="output validation failed",
                error_type=FAILED_CONFIRMED,
            )
        evidence_id = await self._coordinator.confirm_success(
            snapshot,
            validated_payload=validated,
            upstream_request_id=result.upstream_request_id,
            note="confirmed via upstream",
        )
        return ToolResult(status="success", safe_summary="confirmed success", evidence_id=evidence_id)

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #

    async def _finalize_unknown(
        self, logical_call_id: str, normalized: Mapping[str, Any], *, message: str
    ) -> ToolResult:
        self._breaker.record_failure(
            service=self._service.value, internal_tool_name=self.name, arguments=normalized
        )
        await self._coordinator.finalize_unknown(
            logical_call_id=logical_call_id, message=message
        )
        return ToolResult(status="unknown", safe_summary=message, error_type=RESULT_UNKNOWN)

    async def _finalize_definitely_not_sent(
        self,
        context: ToolContext,
        logical_call_id: str,
        normalized: Mapping[str, Any],
        *,
        message: str,
    ) -> ToolResult:
        # 外发前失败同样是上游健康信号：记录到细粒度熔断键，避免对同一调用反复
        # 撞入断连。半开探测失败时 record_failure 会重新打开并清掉 probe_in_flight，
        # 该键不会被永久卡死（见 circuit_breaker.allow 的 half-open 语义）。
        # 注意：熔断打开与余额不足两条路径都提前 return，不会走到这里。
        self._breaker.record_failure(
            service=self._service.value, internal_tool_name=self.name, arguments=normalized
        )
        await self._coordinator.finalize_release(
            logical_call_id=logical_call_id,
            user_id=context.user_id,
            error_type=DEFINITELY_NOT_SENT,
            message=message,
        )
        return ToolResult(status="failed", safe_summary=message, error_type=DEFINITELY_NOT_SENT)

    @staticmethod
    def _error_message(exc: BaseException) -> str:
        return str(exc) or exc.__class__.__name__


__all__ = [
    "DEFINITELY_NOT_SENT",
    "FAILED_CONFIRMED",
    "MCP_POINTS_COST",
    "RESULT_UNKNOWN",
    "AgentMcpAccounting",
    "AgentMcpTool",
    "AgentToolCallSnapshot",
    "DurableToolCallCoordinator",
    "arguments_hash",
    "logical_call_id_for",
]
