from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.models import WalletTransaction
from app.billing.service import ReservationRequest, WalletService
from app.mcp_gateway.contracts import McpCallStatus
from app.mcp_gateway.models import McpCall
from app.mcp_gateway.transport import ToolInvocationOutcome
from app.tasks.models import TaskEvent


MCP_COST = 10


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class McpAccounting:
    """Keeps a call's durable state and point-ledger change in one transaction."""

    def __init__(self, db_session: AsyncSession) -> None:
        self._db = db_session
        self._wallets = WalletService(db_session)

    async def reserve_batch(self, user_id: str, calls: Sequence[McpCall]) -> None:
        requests = tuple(
            ReservationRequest(
                reference_id=call.id,
                idempotency_key=f"mcp:{call.logical_call_id}:reserve",
            )
            for call in calls
            if call.status == McpCallStatus.PLANNED.value
        )
        if requests:
            await self._wallets.reserve_batch(user_id, requests)
        for call in calls:
            if call.status != McpCallStatus.PLANNED.value:
                continue
            transaction = await self._transaction(f"mcp:{call.logical_call_id}:reserve")
            if transaction is None:
                raise RuntimeError("mcp_reservation_ledger_missing")
            call.reservation_transaction_id = transaction.id
            call.status = McpCallStatus.RESERVED.value
            call.updated_at = _now()
        await self._db.flush()

    async def finalize(self, call: McpCall, outcome: ToolInvocationOutcome) -> McpCall:
        if outcome.status == "succeeded":
            return await self.persist_success(call, outcome)
        row = await self._db.scalar(
            select(McpCall)
            .where(McpCall.id == call.id)
            .with_for_update()
        )
        if row is None:
            raise LookupError("mcp_call_not_found")
        if row.status != McpCallStatus.RUNNING.value:
            return row

        now = _now()
        row.upstream_request_id = outcome.upstream_request_id
        row.completed_at = now
        row.updated_at = now
        if outcome.status == "unknown":
            row.status = McpCallStatus.UNKNOWN.value
            row.evidence_json = {"outcome": "unknown"}
            row.error_type = outcome.error_type or "possibly_sent_timeout"
            row.error_message = "MCP outcome could not be confirmed"
            await self._event(row, "mcp_call_unknown")
            await self._db.flush()
            return row

        await self._wallets.release(
            await self._user_id(row),
            MCP_COST,
            f"mcp:{row.logical_call_id}:release",
            row.id,
        )
        transaction = await self._transaction(f"mcp:{row.logical_call_id}:release")
        if transaction is None:
            raise RuntimeError("mcp_release_ledger_missing")
        row.status = McpCallStatus.RELEASED.value
        row.settlement_transaction_id = transaction.id
        row.evidence_json = {"outcome": "failed"}
        if outcome.safe_diagnostic is not None:
            row.evidence_json["output_validation_diagnostic"] = outcome.safe_diagnostic
        row.error_type = outcome.error_type or "upstream_error"
        row.error_message = "MCP call failed"
        await self._event(row, "mcp_call_released")
        await self._db.flush()
        return row

    async def persist_success(
        self, call: McpCall, outcome: ToolInvocationOutcome
    ) -> McpCall:
        row = await self._db.scalar(
            select(McpCall).where(McpCall.id == call.id).with_for_update()
        )
        if row is None:
            raise LookupError("mcp_call_not_found")
        if row.status != McpCallStatus.RUNNING.value:
            return row
        now = _now()
        row.status = McpCallStatus.SUCCEEDED.value
        row.upstream_request_id = outcome.upstream_request_id
        row.response_hash = outcome.response_hash
        row.evidence_json = {
            "outcome": "succeeded",
            "structured_content": outcome.validated_output,
            "upstream_request_id": outcome.upstream_request_id,
        }
        row.error_type = None
        row.error_message = None
        row.completed_at = now
        row.updated_at = now
        await self._db.flush()
        return row

    async def settle_success(self, call: McpCall) -> McpCall:
        row = await self._db.scalar(
            select(McpCall).where(McpCall.id == call.id).with_for_update()
        )
        if row is None:
            raise LookupError("mcp_call_not_found")
        if row.status != McpCallStatus.SUCCEEDED.value:
            return row
        await self._wallets.settle(
            await self._user_id(row),
            MCP_COST,
            f"mcp:{row.logical_call_id}:settle",
            row.id,
        )
        transaction = await self._transaction(f"mcp:{row.logical_call_id}:settle")
        if transaction is None:
            raise RuntimeError("mcp_settlement_ledger_missing")
        row.status = McpCallStatus.SETTLED.value
        row.settlement_transaction_id = transaction.id
        row.updated_at = _now()
        await self._event(row, "mcp_call_settled")
        await self._db.flush()
        return row

    async def _transaction(self, idempotency_key: str) -> WalletTransaction | None:
        return await self._db.scalar(
            select(WalletTransaction).where(WalletTransaction.idempotency_key == idempotency_key)
        )

    async def _user_id(self, call: McpCall) -> str:
        from app.tasks.models import AnalysisTask

        user_id = await self._db.scalar(
            select(AnalysisTask.user_id).where(AnalysisTask.id == call.task_id)
        )
        if user_id is None:
            raise LookupError("analysis_task_not_found")
        return user_id

    async def _event(self, call: McpCall, event_type: str) -> None:
        self._db.add(
            TaskEvent(
                task_id=call.task_id,
                user_id=await self._user_id(call),
                event_type=event_type,
                payload_json={"logical_call_id": call.logical_call_id},
                created_at=_now(),
            )
        )
