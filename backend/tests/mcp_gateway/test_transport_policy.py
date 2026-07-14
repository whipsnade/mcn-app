from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr

from app.mcp_gateway.contracts import DataTapService
from app.mcp_gateway.datatap import DataTapTransport
from app.mcp_gateway.transport import (
    McpUpstreamError,
    PossiblySentTimeout,
    ServiceNotAllowedError,
)


class FakeProtocolSession:
    initialized = 0

    def __init__(self, read_stream, write_stream, **_kwargs) -> None:
        self.service = read_stream

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def initialize(self) -> None:
        type(self).initialized += 1

    async def list_tools(self):
        return SimpleNamespace(
            tools=[
                SimpleNamespace(
                    name=f"{self.service.value}-tool",
                    description="fake",
                    inputSchema={"type": "object", "additionalProperties": False},
                    outputSchema={"type": "object", "additionalProperties": False},
                )
            ]
        )


class ReadTimeoutSession(FakeProtocolSession):
    call_count = 0

    async def call_tool(self, _name, _arguments):
        type(self).call_count += 1
        raise httpx.ReadTimeout("fake read timeout")


@pytest.mark.parametrize(
    "slug", ["zhihu-mcp", "toutiao-mcp", "baidu-index-mcp", "google-trends-mcp"]
)
async def test_disabled_service_is_rejected_before_network(slug: str) -> None:
    opened = AsyncMock(side_effect=AssertionError("network must not open"))
    transport = DataTapTransport(token=SecretStr("unit-test-token"), session_opener=opened)

    with pytest.raises(ServiceNotAllowedError):
        await transport.list_tools(slug)  # type: ignore[arg-type]

    opened.assert_not_awaited()


async def test_raw_allowlisted_string_is_rejected_before_network() -> None:
    opened = AsyncMock(side_effect=AssertionError("network must not open"))
    transport = DataTapTransport(token=SecretStr("unit-test-token"), session_opener=opened)

    with pytest.raises(TypeError):
        await transport.list_tools(DataTapService.AKTOOLS.value)  # type: ignore[arg-type]

    opened.assert_not_awaited()


async def test_all_five_services_use_fixed_https_endpoint_and_bearer_auth() -> None:
    opened: list[tuple[str, httpx.AsyncClient]] = []

    @asynccontextmanager
    async def opener(url: str, *, http_client: httpx.AsyncClient, **_kwargs):
        service = next(item for item in DataTapService if item.value in url)
        opened.append((url, http_client))
        yield service, object(), lambda: f"session-{service.value}"

    transport = DataTapTransport(
        token=SecretStr("unit-test-token"),
        session_opener=opener,
        session_factory=FakeProtocolSession,
    )

    for service in DataTapService:
        tools = await transport.list_tools(service)
        assert tools[0].name == f"{service.value}-tool"

    assert [url for url, _client in opened] == [
        f"https://datatap.deepminer.com.cn/api/gateway/{service.value}/mcp"
        for service in DataTapService
    ]
    for _url, client in opened:
        request = client.build_request("POST", "https://example.invalid")
        assert request.headers["authorization"] == "Bearer unit-test-token"
        assert client.follow_redirects is False
        assert client.timeout.connect is not None
        assert client.timeout.read is not None
        assert client.timeout.write is not None
        assert client.timeout.pool is not None


async def test_redirect_response_is_not_followed() -> None:
    requested: list[str] = []

    def redirect_handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://evil.invalid/mcp"})

    @asynccontextmanager
    async def opener(url: str, *, http_client: httpx.AsyncClient, **_kwargs):
        response = await http_client.get(url)
        response.raise_for_status()
        yield  # pragma: no cover

    transport = DataTapTransport(
        token=SecretStr("unit-test-token"),
        http_transport=httpx.MockTransport(redirect_handler),
        session_opener=opener,
    )

    with pytest.raises(McpUpstreamError):
        await transport.list_tools(DataTapService.INSIGHT_CUBE)

    assert requested == ["https://datatap.deepminer.com.cn/api/gateway/insight-cube-mcp/mcp"]


async def test_read_timeout_after_call_tool_entry_is_possibly_sent_without_retry() -> None:
    @asynccontextmanager
    async def opener(url: str, **_kwargs):
        service = next(item for item in DataTapService if item.value in url)
        yield service, object(), lambda: "session-1"

    ReadTimeoutSession.call_count = 0
    transport = DataTapTransport(
        token=SecretStr("unit-test-token"),
        session_opener=opener,
        session_factory=ReadTimeoutSession,
    )

    with pytest.raises(PossiblySentTimeout):
        await transport.call_tool(DataTapService.BILIBILI, "search", {"keyword": "美妆"})

    assert ReadTimeoutSession.call_count == 1
