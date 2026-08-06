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
from typing import Any, TypedDict
from uuid import uuid4

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.circuit_breaker import FineGrainedCircuitBreaker
from app.agent_runtime.evidence import EvidenceWriter, build_model_evidence_view
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

    @staticmethod
    def _key(call: AgentToolCall, op: str) -> str:
        """账务幂等键：按 dispatch attempt 区分（Gate B P0）。

        第一次派发（dispatch_count=1）保留旧键兼容既有账本；
        第二次及以后按 dispatch:{dispatch_count} 区分，保证每次真实预留/结算。
        """
        if call.dispatch_count is None or call.dispatch_count <= 1:
            return f"agent-mcp:{call.logical_call_id}:{op}"
        return f"agent-mcp:{call.logical_call_id}:dispatch:{call.dispatch_count}:{op}"

    async def reserve(self, user_id: str, call: AgentToolCall) -> None:
        await self._wallets.reserve(
            user_id,
            self.MCP_COST,
            self._key(call, "reserve"),
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
            self._key(call, "settle"),
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
            self._key(call, "release"),
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
                # 2. 查找已有行：幂等回放或 definitely_not_sent 重试。
                existing = await self._by_logical_call_id(db, logical_call_id, for_update=True)
                if existing is not None:
                    # definitely_not_sent 允许一次真实重试（dispatch_count < 2）。
                    if (
                        existing.status == "failed"
                        and existing.error_type == DEFINITELY_NOT_SENT
                        and existing.dispatch_count < 2
                    ):
                        # 归属更新为当前 Step：第二次派发外的恢复/Evidence/lineage
                        # 必须指向本次实际派发的 Step（Gate B P1）。
                        existing.step_id = context.step_id
                        existing.status = "running"
                        existing.dispatch_count += 1
                        existing.error_type = None
                        existing.safe_error_message = None
                        existing.completed_at = None
                        existing.started_at = _now()
                        accounting = AgentMcpAccounting(db)
                        await accounting.reserve(context.user_id, existing)
                        await accounting.mark_running(existing)
                        await db.commit()
                        return None
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
        """成功收口：写 Evidence + settle 10 分，返回 (evidence_id, 统一模型视图)。

        幂等：已 settled 的调用直接回放既有 Evidence（重入不重复写、不重复扣费）。
        返回的视图来自 :func:`build_model_evidence_view`（有界、合法 JSON），
        调用方不得再次执行归一化。
        """
        async with self._session_factory() as db:
            row = await self._require_call(db, logical_call_id, for_update=True)
            if row.status == "settled":
                evidence = await EvidenceWriter(db).get_by_tool_call_id(row.id)
                if evidence is None:
                    raise LookupError("evidence_missing_for_settled_call")
                return evidence.id, build_model_evidence_view(evidence)
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
            return evidence.id, build_model_evidence_view(evidence)

    async def finalize_release(
        self,
        *,
        logical_call_id: str,
        user_id: str,
        error_type: str,
        message: str,
        upstream_request_id: str | None = None,
        retry_exhausted_message: str | None = None,
    ) -> int:
        """失败收口（definitely_not_sent / failed_confirmed）：释放预留。

        在持有调用行锁时读取 ``dispatch_count``，同一事务内决定最终持久化消息：
        ``dispatch_count >= 2`` 且提供了 ``retry_exhausted_message`` 时持久化后者
        （重试已用完的最终反馈），否则持久化 ``message``——保证数据库与实际返回
        一致，绝不提交后再只修正返回对象。返回更新后行的 dispatch_count。
        """
        async with self._session_factory() as db:
            row = await self._require_call(db, logical_call_id, for_update=True)
            if row.status in ("settled", "failed"):
                return row.dispatch_count
            if upstream_request_id:
                row.upstream_request_id = upstream_request_id
            persist_message = message
            if retry_exhausted_message is not None and row.dispatch_count >= 2:
                persist_message = retry_exhausted_message
            await AgentMcpAccounting(db).release(
                user_id, row, error_type=error_type, message=persist_message
            )
            await db.commit()
            return row.dispatch_count

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

    async def finalize_succeeded_empty(
        self,
        *,
        logical_call_id: str,
        user_id: str,
        message: str,
        upstream_request_id: str | None = None,
    ) -> None:
        """succeeded_empty 收口：结算积分 + 标记 failed+succeeded_empty，不写 Evidence。

        上游返回成功但无结构化内容：调用确实成功（结算 10 分），但产物不可用
        （标记 failed + error_type=succeeded_empty），绝不创建内容为 None 的 Evidence。
        回放时返回 failed+succeeded_empty（不变成 success/already settled）。
        """
        async with self._session_factory() as db:
            row = await self._require_call(db, logical_call_id, for_update=True)
            if row.status in ("settled", "failed"):
                return
            if upstream_request_id:
                row.upstream_request_id = upstream_request_id
            await AgentMcpAccounting(db).settle(user_id, row)
            row.status = "failed"
            row.error_type = "succeeded_empty"
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
                return ToolResult(
                    status="success",
                    safe_summary=json.dumps(
                        build_model_evidence_view(evidence), ensure_ascii=False
                    ),
                    evidence_id=evidence.id,
                )
            return ToolResult(status="success", safe_summary="already settled")
        if row.status == "failed":
            feedback = _parse_feedback(row.safe_error_message)
            if feedback is not None:
                # 同指纹重试（幂等回放已有失败行）：同参数不再允许，明确告知。
                feedback["same_fingerprint_retry_allowed"] = False
                if "已尝试过相同参数" not in "".join(feedback.get("suggested_actions", [])):
                    feedback["suggested_actions"] = [
                        "已尝试过相同参数：修改参数、拆分平台或更换工具后重试",
                        *feedback.get("suggested_actions", []),
                    ]
                return ToolResult(
                    status="failed",
                    safe_summary=json.dumps(feedback, ensure_ascii=False),
                    error_type=row.error_type or FAILED_CONFIRMED,
                )
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
            return _feedback_result(
                tool=self.name,
                normalized=raw,
                error_type=DEFINITELY_NOT_SENT,
                request_state="not_created",
                points_state="released",
                retry_allowed=True,
                suggested_actions=["按 input_schema 修正参数后重试"],
            )

        args_hash = arguments_hash(normalized)
        logical_call_id = logical_call_id_for(
            context.run_id, self.name, args_hash)        

        # 细粒度熔断：只封锁相同 service+tool+参数（§11.2）。
        if not self._breaker.allow(
            service=self._service.value, internal_tool_name=self.name, arguments=normalized
        ):
            return _feedback_result(
                tool=self.name,
                normalized=normalized,
                error_type=DEFINITELY_NOT_SENT,
                request_state="blocked",
                points_state="released",
                retry_allowed=False,
                suggested_actions=["熔断封锁相同参数：修改参数、拆分平台或稍后重试"],
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
                logical_call_id, normalized, message=_error_message(exc)
            )
        except _PRE_CONNECTION_ERRORS as exc:
            return await self._finalize_definitely_not_sent(
                context, logical_call_id, normalized, message=_error_message(exc)
            )
        except Exception as exc:
            return await self._finalize_unknown(
                logical_call_id, normalized, message=_error_message(exc)
            )

        if result.is_error:
            self._breaker.record_success(
                service=self._service.value, internal_tool_name=self.name, arguments=normalized
            )
            upstream_message = safe_upstream_text(result.error_text)
            feedback = _feedback_result(
                tool=self.name,
                normalized=normalized,
                error_type=FAILED_CONFIRMED,
                request_state="failed",
                points_state="released",
                retry_allowed=False,
                suggested_actions=["调整参数、拆分平台或更换工具；或继续其他章节"],
                upstream_code=result.upstream_request_id,
                upstream_reason=upstream_message or "upstream reported a business error",
            )
            await self._coordinator.finalize_release(
                logical_call_id=logical_call_id,
                user_id=context.user_id,
                error_type=FAILED_CONFIRMED,
                message=feedback.safe_summary,
                upstream_request_id=result.upstream_request_id,
            )
            # 上游业务错误文本（已脱敏截断）必须回喂模型——"不支持的维度/
            # 不支持的平台"这类信息是模型修正参数的唯一依据，只存库不回喂
            # 会导致模型用同样错误参数反复调用（真实 UAT 已观察到该模式）。
            return feedback

        if result.structured_content is not None:
            try:
                validated = validate_output(result.structured_content, self._output_schema)
            except McpValidationError:
                self._breaker.record_success(
                    service=self._service.value, internal_tool_name=self.name, arguments=normalized
                )
                feedback = _feedback_result(
                    tool=self.name,
                    normalized=normalized,
                    error_type=FAILED_CONFIRMED,
                    request_state="failed",
                    points_state="released",
                    retry_allowed=False,
                    suggested_actions=["查询结果未通过输出 Schema：调整查询维度后重试"],
                    upstream_reason="output validation failed",
                )
                await self._coordinator.finalize_release(
                    logical_call_id=logical_call_id,
                    user_id=context.user_id,
                    error_type=FAILED_CONFIRMED,
                    message=feedback.safe_summary,
                    upstream_request_id=result.upstream_request_id,
                )
                return feedback
        else:
            validated = None

        self._breaker.record_success(
            service=self._service.value, internal_tool_name=self.name, arguments=normalized
        )
        # succeeded_empty：成功但无结构化内容 → 结算 + failed+succeeded_empty，
        # 不创建 None Evidence，不调 finalize_success（Gate B 最终审核 M1）。
        if validated is None:
            feedback = _feedback_result(
                tool=self.name,
                normalized=normalized,
                error_type="succeeded_empty",
                request_state="settled",
                points_state="settled",
                retry_allowed=False,
                suggested_actions=["调整查询参数获取结构化结果，或继续其他章节"],
                upstream_reason="upstream returned no structured content",
            )
            await self._coordinator.finalize_succeeded_empty(
                logical_call_id=logical_call_id,
                user_id=context.user_id,
                message=feedback.safe_summary,
                upstream_request_id=result.upstream_request_id,
            )
            return feedback
        evidence_id, view = await self._coordinator.finalize_success(
            logical_call_id=logical_call_id,
            user_id=context.user_id,
            session_id=context.session_id,
            validated_payload=validated,
            upstream_request_id=result.upstream_request_id,
        )
        # 统一模型视图（有界、合法 JSON）已含 normalization 诊断；不重复归一化、
        # 不中途截断 safe_summary。
        return ToolResult(
            status="success",
            safe_summary=json.dumps(view, ensure_ascii=False),
            evidence_id=evidence_id,
            truncated=bool(view.get("truncated")),
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
        feedback = _feedback_result(
            tool=self.name,
            normalized=normalized,
            error_type=RESULT_UNKNOWN,
            request_state="unknown",
            points_state="reserved",
            retry_allowed=False,
            suggested_actions=["禁止重放同一调用；继续其他工作，结果将自动核对"],
            upstream_reason=message,
        )
        await self._coordinator.finalize_unknown(
            logical_call_id=logical_call_id, message=feedback.safe_summary
        )
        return feedback

    async def _finalize_definitely_not_sent(
        self,
        context: ToolContext,
        logical_call_id: str,
        normalized: Mapping[str, Any],
        *,
        message: str,
    ) -> ToolResult:
        self._breaker.record_failure(
            service=self._service.value, internal_tool_name=self.name, arguments=normalized
        )
        feedback = _feedback_result(
            tool=self.name,
            normalized=normalized,
            error_type=DEFINITELY_NOT_SENT,
            request_state="failed",
            points_state="released",
            retry_allowed=True,
            suggested_actions=["可对相同参数重试一次；仍失败则调整参数、拆分平台或继续其他章节"],
            upstream_reason=message,
        )
        # 第二次 DNR 的最终反馈（retry_allowed=false）在 finalize_release 内与
        # dispatch_count 同锁读取、同事务持久化，保证 DB 与返回一致。
        exhausted = _feedback_result(
            tool=self.name,
            normalized=normalized,
            error_type=DEFINITELY_NOT_SENT,
            request_state="failed",
            points_state="released",
            retry_allowed=False,
            suggested_actions=["已用完一次重试：修改参数、拆分平台或更换工具后重试"],
            upstream_reason=message,
        )
        dispatch_count = await self._coordinator.finalize_release(
            logical_call_id=logical_call_id,
            user_id=context.user_id,
            error_type=DEFINITELY_NOT_SENT,
            message=feedback.safe_summary,
            retry_exhausted_message=exhausted.safe_summary,
        )
        if dispatch_count >= 2:
            return exhausted
        return feedback


def _error_message(exc: BaseException) -> str:
    """异常文本经脱敏：绝不把含凭证/密钥的原始异常文本回喂模型或持久化。"""
    return safe_upstream_text(str(exc) or exc.__class__.__name__)


# --------------------------------------------------------------------------- #
# 结构化失败反馈（Gate B Task 6）：模型拿到的统一决策依据
# --------------------------------------------------------------------------- #


class ToolFailureFeedback(TypedDict):
    tool: str
    arguments_summary: dict[str, Any]
    error_type: str
    upstream_code: str | None
    upstream_reason: str | None
    request_state: str
    points_state: str
    same_fingerprint_retry_allowed: bool
    normalization_status: str | None
    suggested_actions: list[str]


def _arguments_summary(normalized: Mapping[str, Any]) -> dict[str, Any]:
    """参数摘要：截断长值/深结构，只保留模型可读的键值（绝不回灌敏感值）。"""
    summary: dict[str, Any] = {}
    for key, value in normalized.items():
        rendered = json.dumps(value, ensure_ascii=False, default=str)
        summary[key] = rendered if len(rendered) <= 200 else rendered[:200] + "..."
    return summary


def _feedback_result(
    *,
    tool: str,
    normalized: Mapping[str, Any],
    error_type: str,
    request_state: str,
    points_state: str,
    retry_allowed: bool,
    suggested_actions: list[str],
    upstream_code: str | None = None,
    upstream_reason: str | None = None,
    normalization_status: str | None = None,
) -> ToolResult:
    feedback: ToolFailureFeedback = {
        "tool": tool,
        "arguments_summary": _arguments_summary(normalized),
        "error_type": error_type,
        "upstream_code": upstream_code,
        "upstream_reason": upstream_reason,
        "request_state": request_state,
        "points_state": points_state,
        "same_fingerprint_retry_allowed": retry_allowed,
        "normalization_status": normalization_status,
        "suggested_actions": suggested_actions,
    }
    status = "unknown" if error_type == RESULT_UNKNOWN else "failed"
    safe = json.dumps(feedback, ensure_ascii=False)
    # 防止 MEDIUMTEXT 溢出（参数过多/过长时截断建议而非丢弃反馈）
    if len(safe) > 60_000:
        feedback["suggested_actions"] = feedback["suggested_actions"][:2]
        feedback["arguments_summary"] = {
            k: v[:100] for k, v in feedback["arguments_summary"].items()
        }
        safe = json.dumps(feedback, ensure_ascii=False)[:60_000]
    return ToolResult(
        status=status,
        safe_summary=safe,
        error_type=error_type,
    )


def _parse_feedback(safe_error_message: str | None) -> dict[str, Any] | None:
    """尝试解析已持久化的反馈 JSON；非 JSON（旧数据/普通文本）返回 None。"""
    if not safe_error_message:
        return None
    try:
        parsed = json.loads(safe_error_message)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict) or "error_type" not in parsed:
        return None
    return parsed


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
