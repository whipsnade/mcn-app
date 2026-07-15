import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select

from app.billing.models import Wallet, WalletTransaction
from app.billing.service import WalletService
from app.mcp_gateway.contracts import McpCallStatus
from app.mcp_gateway.fake import FakeMcpTransport
from app.mcp_gateway.models import McpCall
from app.mcp_gateway.service import McpGatewayService
from app.mcp_gateway.transport import RemoteToolResult
from app.tasks.models import AnalysisTask
from app.db.session import SessionFactory
from app.identity.models import User
from tests.mcp_gateway.fakes import create_analysis_task
from tests.mcp_gateway.test_billing_lifecycle import MemoryArgumentsLoader, StaticRegistry, command


class SettleWriteFailure(RuntimeError):
    pass


class BlockingFirstTransport(FakeMcpTransport):
    def __init__(self) -> None:
        super().__init__(
            call_result=RemoteToolResult({"items": []}, False, "request-concurrent")
        )
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def call_tool(self, service, remote_name, arguments):
        self.call_count += 1
        if self.call_count == 1:
            self.entered.set()
            await self.release.wait()
        return self.call_result


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
    assert failed_call is not None and failed_call.status == McpCallStatus.SUCCEEDED.value
    assert failed_call.evidence_json == {
        "outcome": "succeeded",
        "structured_content": {"items": []},
        "upstream_request_id": "request-atomicity",
    }
    assert wallet is not None and (wallet.balance, wallet.reserved) == (990, 10)
    assert settle_count == 0
    assert transport.call_count == 1

    monkeypatch.setattr(WalletService, "settle", original_settle)
    recovered = await gateway.execute(call_command)

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


@pytest.mark.asyncio
async def test_existing_running_call_is_not_replayed(db_session, user_factory) -> None:
    task = await create_analysis_task(db_session, user_factory)
    await WalletService(db_session).ensure_welcome_grant(task.user_id)
    transport = FakeMcpTransport()
    gateway = McpGatewayService(
        db_session, transport, arguments_loader=MemoryArgumentsLoader(), registry=StaticRegistry()
    )
    call_command = command(task.user_id, task.id)

    async with db_session.begin_nested():
        row = await gateway._calls.prepare(
            logical_call_id=call_command.logical_call_id,
            user_id=call_command.user_id,
            task_id=call_command.task_id,
            plan_step_id=call_command.plan_step_id,
            internal_tool_name=call_command.internal_tool_name,
            arguments=call_command.arguments,
        )
        await gateway._accounting.reserve_batch(task.user_id, (row,))
    async with db_session.begin_nested():
        await gateway._calls.claim(call_command.logical_call_id)

    repeated = await gateway.execute(call_command)

    assert repeated.status == McpCallStatus.RUNNING.value
    assert transport.call_count == 0


@pytest.mark.asyncio
async def test_concurrent_same_logical_call_only_winner_calls_transport() -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    async with SessionFactory.begin() as setup:
        user = User(
            id=str(uuid4()),
            nickname="并发 MCP 计费",
            role="user",
            status="active",
            created_at=now,
            updated_at=now,
        )
        setup.add(user)
        await setup.flush()

        async def same_user() -> User:
            return user

        task = await create_analysis_task(setup, same_user)
        await WalletService(setup).ensure_welcome_grant(user.id)
        command_to_run = command(user.id, task.id)

    transport = BlockingFirstTransport()

    async def execute_in_own_session():
        async with SessionFactory() as session:
            return await McpGatewayService(
                session,
                transport,
                arguments_loader=MemoryArgumentsLoader(),
                registry=StaticRegistry(),
            ).execute(command_to_run)

    try:
        first = asyncio.create_task(execute_in_own_session())
        await transport.entered.wait()
        second = await asyncio.wait_for(execute_in_own_session(), timeout=2)
        transport.release.set()
        winner = await first

        async with SessionFactory() as verify:
            settled_count = await verify.scalar(
                select(func.count(WalletTransaction.id)).where(
                    WalletTransaction.user_id == user.id,
                    WalletTransaction.kind == "settle",
                )
            )

        assert second.status == McpCallStatus.RUNNING.value
        assert winner.status == McpCallStatus.SETTLED.value
        assert transport.call_count == 1
        assert settled_count == 1
    finally:
        transport.release.set()
        async with SessionFactory.begin() as cleanup:
            await cleanup.execute(delete(McpCall).where(McpCall.task_id == task.id))
            await cleanup.execute(
                delete(WalletTransaction).where(WalletTransaction.user_id == user.id)
            )
            await cleanup.execute(delete(AnalysisTask).where(AnalysisTask.id == task.id))
            await cleanup.execute(delete(User).where(User.id == user.id))
