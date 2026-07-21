import pytest
from sqlalchemy import select

from app.billing.models import Wallet, WalletTransaction
from app.mcp_gateway.transport import McpConnectionError
from app.quick.models import QuickMcpCall


XHS_RESULT = {
    "KOL 列表": [
        {
            "账号ID (kwUid)": "xhs-1",
            "平台": "xiaohongshu",
            "昵称": "预算内达人",
            "粉丝数": 120000,
            "平均互动": 3000.0,
            "互动率-图文笔记": 0.05,
            "综合评分": 88.5,
            "城市": "上海市",
            "预估报价-图文": 8000.0,
            "预估报价-视频": 12000.0,
            "Grow-博主类目标签": ["美食-美食教程"],
        },
        {
            "账号ID (kwUid)": "xhs-2",
            "平台": "xiaohongshu",
            "昵称": "无报价达人",
            "粉丝数": 80000,
            "平均互动": 9000.0,
            "互动率-图文笔记": 0.02,
            "综合评分": 70.0,
            "城市": "杭州市",
            "预估报价-图文": 0,
            "Grow-博主类目标签": [],
        },
        {
            "账号ID (kwUid)": "xhs-3",
            "平台": "xiaohongshu",
            "昵称": "超预算达人",
            "粉丝数": 999000,
            "平均互动": 50000.0,
            "互动率-图文笔记": 0.08,
            "综合评分": 99.0,
            "城市": "北京市",
            "预估报价-图文": 99999.0,
            "Grow-博主类目标签": ["美食"],
        },
    ]
}
DY_RESULT = {
    "KOL 列表": [
        {
            "账号ID (kwUid)": "dy-1",
            "平台": "douyin",
            "昵称": "抖音达人",
            "抖音粉丝数": 560000,
            "平均互动": 6000.0,
            "互动率-日常作品": 0.04,
            "综合评分": 91.2,
            "IP属地": "北京市",
            "官方报价": [{"键": "[官方] 21-60S视频报价", "值": "45000.0"}],
            "预估报价": [{"键": "[预估] 1-20S视频报价", "值": "9000.0"}],
            "Grow-达人类型标签": ["美食-美食探店"],
        },
    ]
}

ALL_ROWS = XHS_RESULT["KOL 列表"] + DY_RESULT["KOL 列表"]


def _search_decision(tool: str, **request_overrides) -> dict:
    request = {"page": 1, "size": 50, "textContentWord": "美食"}
    request.update(request_overrides)
    return {
        "action": "call_tool",
        "internal_tool_name": tool,
        "arguments": {"request": request},
    }


_XHS_SEARCH = "datatap.xiaohongshu.kol.search.v1"
_DY_SEARCH = "datatap.douyin.kol.search.v1"


def _two_platform_decisions() -> list:
    return [
        _search_decision(_XHS_SEARCH),
        _search_decision(_DY_SEARCH),
        {"action": "finish", "result": ALL_ROWS},
    ]


@pytest.mark.asyncio
async def test_kol_recommendations_budget_filter_and_accounting(
    quick_client_factory, db_session
) -> None:
    client, user, transport, _model = await quick_client_factory(
        balance=1000, decisions=_two_platform_decisions()
    )
    transport.results["kol_xiaohongshu_search"] = XHS_RESULT
    transport.results["kol_douyin_search"] = DY_RESULT

    response = await client.get(
        "/api/v1/quick/kol-recommendations",
        params={"budget": 10000, "platforms": "xiaohongshu,douyin"},
    )

    assert response.status_code == 200
    body = response.json()
    # 预算过滤（端点层）：超预算 xhs-3 丢弃；无报价 xhs-2 排最后；其余按互动量降序。
    assert [item["kw_uid"] for item in body["items"]] == ["dy-1", "xhs-1", "xhs-2"]
    dy, xhs1, xhs2 = body["items"]
    assert dy["price"] == 9000.0  # 官方 45000 与预估 9000 取最低有效价
    assert dy["fans"] == 560000
    assert dy["city"] == "北京市"
    assert dy["tags"] == ["美食-美食探店"]
    assert xhs1["price"] == 8000.0
    assert xhs1["engagement_rate"] == 0.05
    assert xhs1["score"] == 88.5
    assert xhs2["price"] is None
    # 记账：2 次模型选择的调用 × 10 积分。
    assert body["points_cost"] == 20
    wallet = await db_session.get(Wallet, user.id)
    assert wallet.balance == 980
    assert wallet.reserved == 0
    rows = list(
        (await db_session.scalars(select(QuickMcpCall).where(QuickMcpCall.user_id == user.id))).all()
    )
    assert len(rows) == 2
    assert {row.status for row in rows} == {"succeeded"}
    assert {row.feature for row in rows} == {"kol_recommend"}
    assert all(row.reserve_transaction_id and row.settlement_transaction_id for row in rows)
    ledger = list(
        (
            await db_session.scalars(
                select(WalletTransaction).where(WalletTransaction.user_id == user.id)
            )
        ).all()
    )
    assert len(ledger) == 4
    assert {tx.reference_type for tx in ledger} == {"quick_mcp_call"}


@pytest.mark.asyncio
async def test_kol_recommendations_model_may_use_mentions_tag(quick_client_factory) -> None:
    from .conftest import MENTIONS_TAG_RESULT

    client, _user, transport, _model = await quick_client_factory(
        decisions=[
            {
                "action": "call_tool",
                "internal_tool_name": "datatap.social.grow.kol.match.mentions.tag.v1",
                "arguments": {
                    "platform": "xiaohongshu",
                    "mentionsTagType": 2002,
                    "keywords": ["美食"],
                },
            },
            _search_decision(_XHS_SEARCH, categoryMentionsTag=["品类提及--美食--美食其他"]),
            {"action": "finish", "result": XHS_RESULT["KOL 列表"]},
        ]
    )
    transport.results["kol_match_mentions_tag"] = MENTIONS_TAG_RESULT
    transport.results["kol_xiaohongshu_search"] = XHS_RESULT

    response = await client.get(
        "/api/v1/quick/kol-recommendations",
        params={"budget": 10000, "platforms": "xiaohongshu"},
    )

    assert response.status_code == 200
    [mentions_args] = transport.called_arguments("kol_match_mentions_tag")
    assert mentions_args["mentionsTagType"] == 2002
    [search_args] = transport.called_arguments("kol_xiaohongshu_search")
    assert search_args["request"]["categoryMentionsTag"] == ["品类提及--美食--美食其他"]
    assert response.json()["points_cost"] == 20


@pytest.mark.asyncio
async def test_kol_recommendations_defaults_to_enabled_channels(quick_client_factory) -> None:
    client, _user, transport, _model = await quick_client_factory(
        channels=("weibo",),
        decisions=[
            _search_decision("datatap.social.grow.kol.weibo.search.v1"),
            {"action": "finish", "result": []},
        ],
    )
    transport.results["kol_weibo_search"] = {"KOL 列表": []}

    response = await client.get(
        "/api/v1/quick/kol-recommendations", params={"budget": 10000}
    )

    assert response.status_code == 200
    assert transport.call_count("kol_weibo_search") == 1
    assert transport.call_count("kol_xiaohongshu_search") == 0


@pytest.mark.asyncio
async def test_kol_recommendations_tool_failure_fed_back(quick_client_factory) -> None:
    client, _user, transport, _model = await quick_client_factory(
        balance=1000,
        decisions=[
            _search_decision(_XHS_SEARCH),
            # 小红书失败后模型换抖音搜索，再 finish。
            _search_decision(_DY_SEARCH),
            {"action": "finish", "result": DY_RESULT["KOL 列表"]},
        ],
    )
    transport.results["kol_xiaohongshu_search"] = McpConnectionError("boom")
    transport.results["kol_douyin_search"] = DY_RESULT

    response = await client.get(
        "/api/v1/quick/kol-recommendations",
        params={"budget": 10000, "platforms": "xiaohongshu,douyin"},
    )

    assert response.status_code == 200
    body = response.json()
    assert {item["platform"] for item in body["items"]} == {"douyin"}


@pytest.mark.asyncio
async def test_kol_recommendations_insufficient_balance_returns_409(
    quick_client_factory, db_session
) -> None:
    client, user, transport, _model = await quick_client_factory(
        balance=5, decisions=[_search_decision(_XHS_SEARCH)]
    )
    transport.results["kol_xiaohongshu_search"] = XHS_RESULT

    response = await client.get(
        "/api/v1/quick/kol-recommendations",
        params={"budget": 10000, "platforms": "xiaohongshu"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "INSUFFICIENT_POINTS"
    wallet = await db_session.get(Wallet, user.id)
    assert wallet.balance == 5
    assert wallet.reserved == 0
    rows = await db_session.scalar(
        select(QuickMcpCall).where(QuickMcpCall.user_id == user.id)
    )
    assert rows is None  # 预留失败不产生留痕


@pytest.mark.asyncio
async def test_kol_recommendations_upstream_failure_releases_reservation(
    quick_client_factory, db_session
) -> None:
    client, user, transport, _model = await quick_client_factory(
        balance=1000,
        decisions=[
            _search_decision(_XHS_SEARCH),
            {"action": "finish", "result": []},
        ],
    )
    transport.results["kol_xiaohongshu_search"] = McpConnectionError("boom")

    response = await client.get(
        "/api/v1/quick/kol-recommendations",
        params={"budget": 10000, "platforms": "xiaohongshu"},
    )

    assert response.status_code == 200
    assert response.json()["items"] == []
    wallet = await db_session.get(Wallet, user.id)
    # 失败调用预留已释放，余额不变。
    assert wallet.balance == 1000
    assert wallet.reserved == 0
    rows = list(
        (await db_session.scalars(select(QuickMcpCall).where(QuickMcpCall.user_id == user.id))).all()
    )
    [search_row] = rows
    assert search_row.internal_tool_name == _XHS_SEARCH
    assert search_row.status == "failed"
    assert search_row.error_type == "connection_error"
    assert search_row.settlement_transaction_id is not None


@pytest.mark.asyncio
async def test_kol_recommendations_requires_auth(client) -> None:
    response = await client.get(
        "/api/v1/quick/kol-recommendations", params={"budget": 10000}
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_kol_recommendations_rejects_invalid_platform(quick_client_factory) -> None:
    client, _user, _transport, _model = await quick_client_factory()
    response = await client.get(
        "/api/v1/quick/kol-recommendations",
        params={"budget": 10000, "platforms": "xiaohongshu,nope"},
    )
    assert response.status_code == 422
