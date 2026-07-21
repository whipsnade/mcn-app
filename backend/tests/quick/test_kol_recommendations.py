import pytest
from sqlalchemy import select

from app.billing.models import Wallet, WalletTransaction
from app.mcp_gateway.transport import McpConnectionError
from app.quick.models import QuickMcpCall

from .conftest import MENTIONS_TAG_RESULT


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


def _prime_search(transport) -> None:
    transport.results["kol_match_mentions_tag"] = MENTIONS_TAG_RESULT
    transport.results["kol_xiaohongshu_search"] = XHS_RESULT
    transport.results["kol_douyin_search"] = DY_RESULT


@pytest.mark.asyncio
async def test_kol_recommendations_arguments_budget_filter_and_accounting(
    quick_client_factory, db_session
) -> None:
    client, user, transport = await quick_client_factory(balance=1000)
    _prime_search(transport)

    response = await client.get(
        "/api/v1/quick/kol-recommendations",
        params={"budget": 10000, "platforms": "xiaohongshu,douyin"},
    )

    assert response.status_code == 200
    body = response.json()
    # 标签匹配：每平台一次，mentionsTagType=2002（品类提及）。
    mentions_args = transport.called_arguments("kol_match_mentions_tag")
    assert len(mentions_args) == 2
    assert {args["platform"] for args in mentions_args} == {"xiaohongshu", "douyin"}
    assert all(args["mentionsTagType"] == 2002 for args in mentions_args)
    assert all(args["keywords"] == ["美食"] for args in mentions_args)
    # 搜索：categoryMentionsTag + size=50。
    for remote in ("kol_xiaohongshu_search", "kol_douyin_search"):
        [search_args] = transport.called_arguments(remote)
        assert search_args["request"]["size"] == 50
        assert search_args["request"]["categoryMentionsTag"] == ["品类提及--美食--美食其他"]
    # 预算过滤：超预算 xhs-3 丢弃；无报价 xhs-2 排最后；其余按互动量降序。
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
    # 记账：4 次调用 × 10 积分。
    assert body["points_cost"] == 40
    wallet = await db_session.get(Wallet, user.id)
    assert wallet.balance == 960
    assert wallet.reserved == 0
    rows = list(
        (await db_session.scalars(select(QuickMcpCall).where(QuickMcpCall.user_id == user.id))).all()
    )
    assert len(rows) == 4
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
    assert len(ledger) == 8
    assert {tx.reference_type for tx in ledger} == {"quick_mcp_call"}


@pytest.mark.asyncio
async def test_kol_recommendations_reuses_cached_tags(quick_client_factory) -> None:
    client, _user, transport = await quick_client_factory(balance=1000)
    _prime_search(transport)

    first = await client.get(
        "/api/v1/quick/kol-recommendations",
        params={"budget": 10000, "platforms": "xiaohongshu,douyin"},
    )
    second = await client.get(
        "/api/v1/quick/kol-recommendations",
        params={"budget": 10000, "platforms": "xiaohongshu"},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert transport.call_count("kol_match_mentions_tag") == 2  # 缓存生效，未重复计费
    assert second.json()["points_cost"] == 10


@pytest.mark.asyncio
async def test_kol_recommendations_falls_back_to_text_content_word(
    quick_client_factory,
) -> None:
    client, _user, transport = await quick_client_factory(industries=("美食", "美妆"))
    transport.results["kol_match_mentions_tag"] = {"标签匹配结果列表": [{"标签集合": []}]}
    transport.results["kol_xiaohongshu_search"] = XHS_RESULT

    response = await client.get(
        "/api/v1/quick/kol-recommendations",
        params={"budget": 10000, "platforms": "xiaohongshu"},
    )

    assert response.status_code == 200
    [search_args] = transport.called_arguments("kol_xiaohongshu_search")
    assert search_args["request"]["textContentWord"] == "美食"
    assert "categoryMentionsTag" not in search_args["request"]


@pytest.mark.asyncio
async def test_kol_recommendations_defaults_to_enabled_channels(quick_client_factory) -> None:
    client, _user, transport = await quick_client_factory(channels=("weibo",))
    _prime_search(transport)
    transport.results["kol_weibo_search"] = {"KOL 列表": []}

    response = await client.get(
        "/api/v1/quick/kol-recommendations", params={"budget": 10000}
    )

    assert response.status_code == 200
    assert transport.call_count("kol_weibo_search") == 1
    assert transport.call_count("kol_xiaohongshu_search") == 0
    # B站/微博/微信：request 包装但无 categoryMentionsTag，行业走 textContentWord，
    # 不做标签匹配调用。
    [weibo_args] = transport.called_arguments("kol_weibo_search")
    assert weibo_args == {"request": {"page": 1, "size": 50, "textContentWord": "美食"}}
    assert transport.call_count("kol_match_mentions_tag") == 0


@pytest.mark.asyncio
async def test_kol_recommendations_tolerates_single_platform_failure(quick_client_factory) -> None:
    client, _user, transport = await quick_client_factory(balance=1000)
    transport.results["kol_match_mentions_tag"] = MENTIONS_TAG_RESULT
    transport.results["kol_xiaohongshu_search"] = XHS_RESULT
    transport.results["kol_douyin_search"] = McpConnectionError("boom")

    response = await client.get(
        "/api/v1/quick/kol-recommendations",
        params={"budget": 10000, "platforms": "xiaohongshu,douyin"},
    )

    # 抖音失败不拖垮整体：仍返回小红书的达人。
    assert response.status_code == 200
    body = response.json()
    assert body["items"]
    assert {item["platform"] for item in body["items"]} == {"xiaohongshu"}


@pytest.mark.asyncio
async def test_kol_recommendations_all_platforms_failed_returns_502(quick_client_factory) -> None:
    client, _user, transport = await quick_client_factory(balance=1000)
    transport.results["kol_match_mentions_tag"] = MENTIONS_TAG_RESULT
    transport.results["kol_xiaohongshu_search"] = McpConnectionError("boom")
    transport.results["kol_douyin_search"] = McpConnectionError("boom")

    response = await client.get(
        "/api/v1/quick/kol-recommendations",
        params={"budget": 10000, "platforms": "xiaohongshu,douyin"},
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "QUICK_CALL_FAILED"


@pytest.mark.asyncio
async def test_kol_recommendations_insufficient_balance_returns_409(
    quick_client_factory, db_session
) -> None:
    client, user, transport = await quick_client_factory(balance=5)
    _prime_search(transport)

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
async def test_kol_recommendations_upstream_failure_returns_502_and_releases(
    quick_client_factory, db_session
) -> None:
    client, user, transport = await quick_client_factory(balance=1000)
    transport.results["kol_match_mentions_tag"] = MENTIONS_TAG_RESULT
    transport.results["kol_xiaohongshu_search"] = McpConnectionError("boom")

    response = await client.get(
        "/api/v1/quick/kol-recommendations",
        params={"budget": 10000, "platforms": "xiaohongshu"},
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "QUICK_CALL_FAILED"
    wallet = await db_session.get(Wallet, user.id)
    # 标签匹配已结算（10），搜索预留已释放。
    assert wallet.balance == 990
    assert wallet.reserved == 0
    rows = list(
        (await db_session.scalars(select(QuickMcpCall).where(QuickMcpCall.user_id == user.id))).all()
    )
    by_tool = {row.internal_tool_name: row for row in rows}
    search_row = by_tool["datatap.xiaohongshu.kol.search.v1"]
    assert search_row.status == "failed"
    assert search_row.error_type == "connection_error"
    assert search_row.settlement_transaction_id is not None
    assert by_tool["datatap.social.grow.kol.match.mentions.tag.v1"].status == "succeeded"


@pytest.mark.asyncio
async def test_kol_recommendations_requires_auth(client) -> None:
    response = await client.get(
        "/api/v1/quick/kol-recommendations", params={"budget": 10000}
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_kol_recommendations_rejects_invalid_platform(quick_client_factory) -> None:
    client, _user, _transport = await quick_client_factory()
    response = await client.get(
        "/api/v1/quick/kol-recommendations",
        params={"budget": 10000, "platforms": "xiaohongshu,nope"},
    )
    assert response.status_code == 422
