from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.billing.models import Wallet, WalletTransaction
from app.billing.service import InsufficientPointsError, WalletService
from app.mcp_gateway.contracts import DataTapService, McpCallStatus
from app.mcp_gateway.fake import FakeMcpTransport
from app.mcp_gateway.registry import ApprovedTool
from app.mcp_gateway.service import ExecuteMcpCall, McpGatewayService
from app.mcp_gateway.transport import PossiblySentTimeout, RemoteToolResult
from tests.mcp_gateway.fakes import create_analysis_task, strict_object_schema


INPUT_SCHEMA = strict_object_schema({"keyword": {"type": "string"}}, "keyword")
OUTPUT_SCHEMA = strict_object_schema({"items": {"type": "array", "items": {}}}, "items")


class StaticRegistry:
    def __init__(self) -> None:
        self.approved = ApprovedTool(
            catalog_id="catalog-1",
            internal_name="creator.search.v1",
            service=DataTapService.SOCIAL_GROW,
            remote_name="search-creators",
            input_schema=INPUT_SCHEMA,
            output_schema=OUTPUT_SCHEMA,
        )

    async def require_enabled(self, internal_name: str) -> ApprovedTool:
        assert internal_name == self.approved.internal_name
        return self.approved


class MemoryArgumentsLoader:
    async def load_arguments(self, *, task_id: str, plan_step_id: str) -> dict:
        return {"keyword": "美妆"}


def command(user_id: str, task_id: str, *, logical_call_id: str | None = None) -> ExecuteMcpCall:
    return ExecuteMcpCall(
        logical_call_id=logical_call_id or str(uuid4()),
        user_id=user_id,
        task_id=task_id,
        plan_step_id=f"step-{uuid4()}",
        internal_tool_name="creator.search.v1",
        arguments={"keyword": "美妆"},
    )


@pytest.mark.asyncio
async def test_success_charges_ten_exactly_once(db_session, user_factory) -> None:
    task = await create_analysis_task(db_session, user_factory)
    await WalletService(db_session).ensure_welcome_grant(task.user_id)
    transport = FakeMcpTransport(
        call_result=RemoteToolResult({"items": []}, False, "request-success")
    )
    gateway = McpGatewayService(
        db_session, transport, arguments_loader=MemoryArgumentsLoader(), registry=StaticRegistry()
    )
    call_command = command(task.user_id, task.id)

    first = await gateway.execute(call_command)
    second = await gateway.execute(call_command)
    wallet = await db_session.get(Wallet, task.user_id)

    assert first.status == second.status == McpCallStatus.SETTLED.value
    assert wallet is not None and (wallet.balance, wallet.reserved) == (990, 0)
    assert transport.call_count == 1


@pytest.mark.asyncio
async def test_batch_success_charges_each_call_once(db_session, user_factory) -> None:
    task = await create_analysis_task(db_session, user_factory)
    await WalletService(db_session).ensure_welcome_grant(task.user_id)
    transport = FakeMcpTransport(
        call_result=RemoteToolResult({"items": []}, False, "request-batch-success")
    )
    gateway = McpGatewayService(
        db_session, transport, arguments_loader=MemoryArgumentsLoader(), registry=StaticRegistry()
    )

    calls = await gateway.execute_batch(tuple(command(task.user_id, task.id) for _ in range(2)))
    wallet = await db_session.get(Wallet, task.user_id)

    assert tuple(call.status for call in calls) == (McpCallStatus.SETTLED.value,) * 2
    assert wallet is not None and (wallet.balance, wallet.reserved) == (980, 0)
    assert transport.call_count == 2


@pytest.mark.asyncio
async def test_failure_releases_and_unknown_retains_reservation(db_session, user_factory) -> None:
    task = await create_analysis_task(db_session, user_factory)
    await WalletService(db_session).ensure_welcome_grant(task.user_id)

    failed = await McpGatewayService(
        db_session,
        FakeMcpTransport(call_error=RuntimeError("upstream")),
        arguments_loader=MemoryArgumentsLoader(),
        registry=StaticRegistry(),
    ).execute(command(task.user_id, task.id))
    wallet = await db_session.get(Wallet, task.user_id)
    assert failed.status == McpCallStatus.RELEASED.value
    assert wallet is not None and (wallet.balance, wallet.reserved) == (1000, 0)

    unknown = await McpGatewayService(
        db_session,
        FakeMcpTransport(call_error=PossiblySentTimeout("timeout")),
        arguments_loader=MemoryArgumentsLoader(),
        registry=StaticRegistry(),
    ).execute(command(task.user_id, task.id))
    wallet = await db_session.get(Wallet, task.user_id)
    assert unknown.status == McpCallStatus.UNKNOWN.value
    assert wallet is not None and (wallet.balance, wallet.reserved) == (990, 10)


@pytest.mark.asyncio
async def test_insufficient_batch_never_calls_upstream(db_session, user_factory) -> None:
    task = await create_analysis_task(db_session, user_factory)
    wallets = WalletService(db_session)
    await wallets.ensure_welcome_grant(task.user_id)
    wallet = await db_session.get(Wallet, task.user_id)
    assert wallet is not None
    wallet.balance = 20
    await db_session.flush()
    transport = FakeMcpTransport()
    gateway = McpGatewayService(
        db_session, transport, arguments_loader=MemoryArgumentsLoader(), registry=StaticRegistry()
    )

    with pytest.raises(InsufficientPointsError):
        await gateway.execute_batch(tuple(command(task.user_id, task.id) for _ in range(3)))

    reserve_count = await db_session.scalar(
        select(func.count(WalletTransaction.id)).where(
            WalletTransaction.user_id == task.user_id,
            WalletTransaction.kind == "reserve",
        )
    )
    assert transport.call_count == 0
    assert (wallet.balance, wallet.reserved) == (20, 0)
    assert reserve_count == 0
