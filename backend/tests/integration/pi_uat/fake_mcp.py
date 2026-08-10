"""离线进程级 UAT 的 fake DataTap MCP 服务（真实 Streamable HTTP MCP 协议）。

每个服务只提供测试明确登记的已审核工具，返回 DataTap 同构
``{"result": "<json-string>"}`` 结构化内容；全部调用记入 ``calls`` 供
0-外发/参数断言。不访问任何外部地址。
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.routing import Mount

# 品牌场景fixture：与 Builder canonical 口径一致的行形状（中文业务键）。
OVERVIEW_ROWS = [
    {"平台": "小红书", "声量": 120, "互动数": 240, "发帖数": 8},
    {"平台": "抖音", "声量": 200, "互动数": 360, "发帖数": 15},
]
SENTIMENT_ROWS = [
    {"平台": "小红书", "情感": "正面", "声量": 80},
    {"平台": "小红书", "情感": "中性", "声量": 30},
    {"平台": "小红书", "情感": "负面", "声量": 10},
]
TREND_ROWS = [
    {"日期": "2026-07-01", "平台": "小红书", "声量": 10, "互动数": 20},
    {"日期": "2026-07-02", "平台": "小红书", "声量": 12, "互动数": 22},
]
TOPIC_ROWS = [
    {"平台": "小红书", "话题": "新品试色", "声量": 66},
]
TOP_POST_ROWS = [
    {
        "平台": "小红书",
        "帖子ID": "post-1",
        "标题": "试色笔记",
        "链接": "https://example.invalid/post-1",
        "作者": " tester ",
        "发布时间": "2026-07-01",
        "互动数": 20,
    }
]
KOL_ROWS = [
    {
        "平台": "小红书",
        "达人ID": "kol-1",
        "昵称": "美妆达人A",
        "粉丝数": 120000,
        "报价": 8000,
        "互动率": 0.06,
    }
]

_SERVICE_TOOLS: dict[str, dict[str, list[dict[str, Any]]]] = {
    "insight-cube-mcp": {
        "social_statistic_overview": OVERVIEW_ROWS,
        "social_statistic_trend": TREND_ROWS,
        "query_analysis_data": OVERVIEW_ROWS,
        "social_statistic_hot_topic": TOPIC_ROWS,
        "query_raw_posts": TOP_POST_ROWS,
        "social_statistic_user_profile": [{"平台": "小红书", "年龄段": "25-29", "占比": 0.4}],
    },
    "social-grow-mcp": {
        "kol_xiaohongshu_search": KOL_ROWS,
    },
}


def _datatap_payload(rows: list[dict[str, Any]]) -> dict[str, str]:
    return {"result": json.dumps({"rows": rows}, ensure_ascii=False)}


class FakeDataTapService:
    """一个 fake DataTap 服务实例；``calls`` 记录 (tool, arguments)。"""

    def __init__(self, service: str) -> None:
        if service not in _SERVICE_TOOLS:
            raise ValueError(f"unknown fake service: {service}")
        self.service = service
        self.calls: list[dict[str, Any]] = []
        self.server = FastMCP(service, streamable_http_path="/mcp", stateless_http=True)
        for tool_name, rows in _SERVICE_TOOLS[service].items():
            self._register(tool_name, rows)

    def _register(self, tool_name: str, rows: list[dict[str, Any]]) -> None:
        record = self.calls

        async def tool_impl(
            brand: str = "",
            keyword: str = "",
            platform: str = "",
            datasource: str = "",
            period: str = "",
        ) -> dict[str, str]:
            record.append(
                {
                    "tool": tool_name,
                    "arguments": {
                        "brand": brand,
                        "keyword": keyword,
                        "platform": platform,
                        "datasource": datasource,
                        "period": period,
                    },
                }
            )
            return _datatap_payload(rows)

        tool_impl.__name__ = tool_name
        self.server.tool(name=tool_name, description=f"fake {tool_name}")(tool_impl)

    def app(self) -> Starlette:
        return self.server.streamable_http_app()


def datatap_gateway_app(services: list[FakeDataTapService]) -> Starlette:
    """把多个 fake 服务挂到与生产一致的 per-service 路径下。

    ``/api/gateway/<service-slug>/mcp`` 与 DataTapTransport._endpoint 的
    路径契约一致，离线拓扑用单一 loopback origin 承载全部 fake 服务。
    Mount 不会运行子应用 lifespan，因此父应用统一启动各服务的 MCP
    session manager（task group）。
    """
    import contextlib
    from collections.abc import AsyncIterator

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with contextlib.AsyncExitStack() as stack:
            for service in services:
                await stack.enter_async_context(service.server.session_manager.run())
            yield

    return Starlette(
        routes=[
            Mount(f"/api/gateway/{service.service}", app=service.app())
            for service in services
        ],
        lifespan=lifespan,
    )
