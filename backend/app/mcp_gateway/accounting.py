from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.models import TenantWalletTransaction, WalletTransaction
from app.billing.service import ReservationRequest, WalletService
from app.mcp_gateway.contracts import McpCallStatus
from app.mcp_gateway.models import McpCall
from app.mcp_gateway.transport import ToolInvocationOutcome


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
            transaction = await self._transaction(
                f"mcp:{call.logical_call_id}:reserve", call_id=call.id, kind="reserve"
            )
            if transaction is None:
                raise RuntimeError("mcp_reservation_ledger_missing")
            # The legacy McpCall foreign key points at wallet_transactions.  A
            # tenant-backed call is linked by tenant_wallet_transactions.tool_call_id
            # instead, so never insert a tenant ledger id into the old FK column.
            if isinstance(transaction, WalletTransaction):
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
            user_id = await self._user_id(row)
            tenant = await self._wallets._tenant_wallet(user_id)
            if tenant is not None:
                permit_id = await self._db.scalar(
                    select(TenantWalletTransaction.id)
                    .where(
                        TenantWalletTransaction.tenant_id == tenant[0],
                        TenantWalletTransaction.user_id == user_id,
                        TenantWalletTransaction.tool_call_id == row.id,
                        TenantWalletTransaction.kind == "reserve",
                    )
                    .order_by(TenantWalletTransaction.created_at.desc())
                    .limit(1)
                )
                if permit_id is not None:
                    from app.pi_gateway.accounting import TenantAccountingService

                    await TenantAccountingService(self._db).fail_mcp_call(
                        permit_id, "result_unknown"
                    )
            await self._db.flush()
            return row

        await self._wallets.release(
            await self._user_id(row),
            MCP_COST,
            f"mcp:{row.logical_call_id}:release",
            row.id,
        )
        transaction = await self._transaction(
            f"mcp:{row.logical_call_id}:release", call_id=row.id, kind="release"
        )
        if transaction is None:
            raise RuntimeError("mcp_release_ledger_missing")
        row.status = McpCallStatus.RELEASED.value
        if isinstance(transaction, WalletTransaction):
            row.settlement_transaction_id = transaction.id
        row.evidence_json = {"outcome": "failed"}
        if outcome.safe_diagnostic is not None:
            row.evidence_json["output_validation_diagnostic"] = outcome.safe_diagnostic
        if outcome.error_message is not None:
            row.evidence_json["upstream_error_message"] = outcome.error_message
        row.error_type = outcome.error_type or "upstream_error"
        row.error_message = outcome.error_message or "MCP call failed"
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
            "outcome": (
                "succeeded_empty" if outcome.result_status == "empty"
                else "result_unavailable" if outcome.result_status == "unavailable"
                else "succeeded"
            ),
            "structured_content": outcome.validated_output,
            "upstream_request_id": outcome.upstream_request_id,
        }
        row.error_type = (
            "succeeded_empty" if outcome.result_status == "empty"
            else "result_unavailable" if outcome.result_status == "unavailable"
            else None
        )
        row.error_message = row.error_type
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
        transaction = await self._transaction(
            f"mcp:{row.logical_call_id}:settle", call_id=row.id, kind="settle"
        )
        if transaction is None:
            raise RuntimeError("mcp_settlement_ledger_missing")
        row.status = McpCallStatus.SETTLED.value
        if isinstance(transaction, WalletTransaction):
            row.settlement_transaction_id = transaction.id
        row.updated_at = _now()
        await self._db.flush()
        return row

    async def _transaction(
        self,
        idempotency_key: str,
        *,
        call_id: str | None = None,
        kind: str | None = None,
    ) -> WalletTransaction | TenantWalletTransaction | None:
        legacy = await self._db.scalar(
            select(WalletTransaction).where(WalletTransaction.idempotency_key == idempotency_key)
        )
        if legacy is not None:
            return legacy
        if call_id is None or kind is None:
            return None
        return await self._db.scalar(
            select(TenantWalletTransaction)
            .where(
                TenantWalletTransaction.tool_call_id == call_id,
                TenantWalletTransaction.kind == kind,
            )
            .order_by(TenantWalletTransaction.created_at.desc())
            .limit(1)
        )

    async def _user_id(self, call: McpCall) -> str:
        from app.tasks.models import AnalysisTask

        user_id = await self._db.scalar(
            select(AnalysisTask.user_id).where(AnalysisTask.id == call.task_id)
        )
        if user_id is None:
            raise LookupError("analysis_task_not_found")
        return user_id
