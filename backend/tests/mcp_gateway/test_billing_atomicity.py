import pytest
from sqlalchemy import func, select

from app.billing.models import Wallet, WalletTransaction
from app.billing.service import WalletService
from app.mcp_gateway.accounting import McpAccounting
from app.mcp_gateway.contracts import McpCallStatus
from app.mcp_gateway.fake import FakeMcpTransport
from app.mcp_gateway.models import McpCall
from app.mcp_gateway.service import McpGatewayService
from app.mcp_gateway.transport import RemoteToolResult, ToolInvocationOutcome
from tests.mcp_gateway.fakes import create_analysis_task
from tests.mcp_gateway.test_billing_lifecycle import MemoryArgumentsLoader, StaticRegistry, command


class SettleWriteFailure(RuntimeError):
    pass


@pytest.mark.asyncio
async def test_settlement_rollback_can_be_recovered_without_replaying_mcp(
    db_session, user_factory, monkeypatch
) -> None:
    task = await create_analysis_task(db_session, user_factory)
    await WalletService(db_session).ensure_welcome_grant(task.user_id)
    transport = FakeMcpTransport(
        call_result=RemoteToolResult({"items": []}, False, "request-atomicity")
    )
    gateway = McpGatewayService(
        db_session, transport, arguments_loader=MemoryArgumentsLoader(), registry=StaticRegistry()
    )
    call_command = command(task.user_id, task.id)
    task_id = task.id
    user_id = task.user_id
    original_settle = WalletService.settle

    async def settle_then_fail(self, *args, **kwargs):
        await original_settle(self, *args, **kwargs)
        raise SettleWriteFailure()

    monkeypatch.setattr(WalletService, "settle", settle_then_fail)
    with pytest.raises(SettleWriteFailure):
        await gateway.execute(call_command)

    db_session.expire_all()
    failed_call = await db_session.scalar(
        select(McpCall).where(
            McpCall.logical_call_id == call_command.logical_call_id,
            McpCall.task_id == task_id,
        )
    )
    wallet = await db_session.get(Wallet, user_id)
    settle_count = await db_session.scalar(
        select(func.count(WalletTransaction.id)).where(
            WalletTransaction.user_id == user_id,
            WalletTransaction.kind == "settle",
        )
    )
    assert failed_call is not None and failed_call.status == McpCallStatus.RUNNING.value
    assert wallet is not None and (wallet.balance, wallet.reserved) == (990, 10)
    assert settle_count == 0
    assert transport.call_count == 1

    monkeypatch.setattr(WalletService, "settle", original_settle)
    async with db_session.begin_nested():
        recovered = await McpAccounting(db_session).finalize(
            failed_call,
            ToolInvocationOutcome(
                "succeeded", {"items": []}, "a" * 64, "request-atomicity", None
            ),
        )

    wallet = await db_session.get(Wallet, user_id)
    settled_after_recovery = await db_session.scalar(
        select(func.count(WalletTransaction.id)).where(
            WalletTransaction.user_id == user_id,
            WalletTransaction.kind == "settle",
        )
    )
    assert recovered.status == McpCallStatus.SETTLED.value
    assert wallet is not None and (wallet.balance, wallet.reserved) == (990, 0)
    assert settled_after_recovery == 1
    assert transport.call_count == 1
