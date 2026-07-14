from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from app.mcp_gateway.contracts import DataTapService
from app.mcp_gateway.datatap import DataTapTransport
from app.mcp_gateway.transport import McpUpstreamError


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class IsolatedSession:
    failing: set[DataTapService] = set()
    attempts: dict[DataTapService, int] = {}

    def __init__(self, read_stream, write_stream, **_kwargs) -> None:
        self.service = read_stream

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def initialize(self) -> None:
        return None

    async def list_tools(self):
        self.attempts[self.service] = self.attempts.get(self.service, 0) + 1
        if self.service in self.failing:
            raise McpUpstreamError("fake upstream failure")
        return SimpleNamespace(
            tools=[
                SimpleNamespace(
                    name=f"{self.service.value}-tool",
                    description=None,
                    inputSchema={"type": "object", "additionalProperties": False},
                    outputSchema={"type": "object", "additionalProperties": False},
                )
            ]
        )


def isolated_transport(clock: Clock) -> DataTapTransport:
    @asynccontextmanager
    async def opener(url: str, **_kwargs):
        service = next(item for item in DataTapService if item.value in url)
        yield service, object(), lambda: None

    return DataTapTransport(
        token=SecretStr("unit-test-token"),
        session_opener=opener,
        session_factory=IsolatedSession,
        failure_threshold=2,
        circuit_reset_seconds=10,
        clock=clock,
    )


async def test_open_circuit_for_one_service_does_not_block_another() -> None:
    IsolatedSession.failing = {DataTapService.AKTOOLS}
    IsolatedSession.attempts = {}
    transport = isolated_transport(Clock())

    for _ in range(transport.failure_threshold):
        with pytest.raises(McpUpstreamError):
            await transport.list_tools(DataTapService.AKTOOLS)
    with pytest.raises(McpUpstreamError, match="circuit"):
        await transport.list_tools(DataTapService.AKTOOLS)

    tools = await transport.list_tools(DataTapService.BILIBILI)
    assert tools
    assert IsolatedSession.attempts[DataTapService.AKTOOLS] == 2
    assert IsolatedSession.attempts[DataTapService.BILIBILI] == 1


async def test_half_open_probe_is_per_service_and_resets_only_its_circuit() -> None:
    clock = Clock()
    IsolatedSession.failing = {DataTapService.AKTOOLS}
    IsolatedSession.attempts = {}
    transport = isolated_transport(clock)
    for _ in range(transport.failure_threshold):
        with pytest.raises(McpUpstreamError):
            await transport.list_tools(DataTapService.AKTOOLS)

    clock.advance(10)
    IsolatedSession.failing.clear()
    tools = await transport.list_tools(DataTapService.AKTOOLS)

    assert tools
    assert await transport.list_tools(DataTapService.BILIBILI)


async def test_busy_service_semaphore_does_not_delay_another_service() -> None:
    aktools_started = asyncio.Event()
    release_aktools = asyncio.Event()

    @asynccontextmanager
    async def opener(url: str, **_kwargs):
        service = next(item for item in DataTapService if item.value in url)
        if service is DataTapService.AKTOOLS:
            aktools_started.set()
            await release_aktools.wait()
        yield service, object(), lambda: None

    transport = DataTapTransport(
        token=SecretStr("unit-test-token"),
        session_opener=opener,
        session_factory=IsolatedSession,
        max_concurrency_per_service=1,
    )
    blocked = asyncio.create_task(transport.list_tools(DataTapService.AKTOOLS))
    await aktools_started.wait()

    bilibili = await asyncio.wait_for(transport.list_tools(DataTapService.BILIBILI), timeout=0.2)
    release_aktools.set()
    await blocked

    assert bilibili
