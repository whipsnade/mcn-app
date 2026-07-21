import pytest
from sqlalchemy import select

from app.billing.models import Wallet
from app.quick.models import QuickMcpCall


DETAIL_RESULT = {
    "详情列表": [
        {
            "账号ID (kwUid)": "xhs-1",
            "发帖数据-汇总统计": {"作品数": 53, "平均互动": 695242.25},
            "受众画像": {"粉丝性别分布": [{"键": "女", "值": 0.6}]},
            "价格趋势": [{"日期": "2026-07-01", "报价": 8000.0}],
        }
    ]
}
POSTS_RESULT = {
    "帖子列表": [
        {
            "标题": f"热帖{i}",
            "用户昵称": "美食小达人",
            "互动数": 15000 - i,
            "点赞数": 12000,
            "评论数": 2000,
            "收藏数": 1000,
            "发布时间": "2026-07-10 12:00:00",
            "帖子链接": f"https://example.com/p/{i}",
        }
        for i in range(12)
    ]
}

_DETAIL_TOOL = "datatap.social.grow.kol.detail.v1"
_RAW_POSTS_TOOL = "datatap.insight.query.raw.posts.v1"


def _detail_decision() -> dict:
    return {
        "action": "call_tool",
        "internal_tool_name": _DETAIL_TOOL,
        "arguments": {
            "platform": "xiaohongshu",
            "kwUidList": ["xhs-1"],
            "scope": ["fansAudience", "postSummaryStatistics", "priceTrend"],
        },
    }


def _posts_decision() -> dict:
    return {
        "action": "call_tool",
        "internal_tool_name": _RAW_POSTS_TOOL,
        "arguments": {
            "target_type": "field",
            "field_name": "用户昵称",
            "field_value": ["美食小达人"],
            "datasource": ["小红书"],
            "start_time": "2026-06-21",
            "end_time": "2026-07-21",
            "order_by": "互动数",
            "size": 10,
        },
    }


def _finish(*, posts: list, posts_degraded: bool = False) -> dict:
    result = {"detail": DETAIL_RESULT["详情列表"][0], "posts": posts}
    if posts_degraded:
        result["posts_degraded"] = True
    return {"action": "finish", "result": result}


@pytest.mark.asyncio
async def test_kol_detail_model_driven_flow(quick_client_factory) -> None:
    client, _user, transport, _model = await quick_client_factory(
        decisions=[
            _detail_decision(),
            _posts_decision(),
            _finish(posts=POSTS_RESULT["帖子列表"]),
        ]
    )
    transport.results["kol_detail"] = DETAIL_RESULT
    transport.results["query_raw_posts"] = POSTS_RESULT

    response = await client.get(
        "/api/v1/quick/kol-detail",
        params={"platform": "xiaohongshu", "kw_uid": "xhs-1", "nickname": "美食小达人"},
    )

    assert response.status_code == 200
    body = response.json()
    # 工具按模型选择执行。
    [detail_args] = transport.called_arguments("kol_detail")
    assert detail_args["kwUidList"] == ["xhs-1"]
    [posts_args] = transport.called_arguments("query_raw_posts")
    assert posts_args["field_value"] == ["美食小达人"]
    assert body["detail"]["账号ID (kwUid)"] == "xhs-1"
    assert body["detail"]["受众画像"]["粉丝性别分布"] == [{"键": "女", "值": 0.6}]
    assert len(body["posts"]) == 10  # 截断到 10 条
    first = body["posts"][0]
    assert first == {
        "title": "热帖0",
        "nickname": "美食小达人",
        "interact": 15000.0,
        "like": 12000.0,
        "comment": 2000.0,
        "collect": 1000.0,
        "publish_time": "2026-07-10 12:00:00",
        "url": "https://example.com/p/0",
        "platform": "xiaohongshu",
    }
    assert body["posts_degraded"] is False
    assert body["points_cost"] == 20


@pytest.mark.asyncio
async def test_kol_detail_posts_failure_model_marks_degraded(quick_client_factory) -> None:
    from app.mcp_gateway.transport import McpConnectionError

    client, _user, transport, _model = await quick_client_factory(
        balance=1000,
        decisions=[
            _detail_decision(),
            _posts_decision(),
            _finish(posts=[], posts_degraded=True),
        ],
    )
    transport.results["kol_detail"] = DETAIL_RESULT
    transport.results["query_raw_posts"] = McpConnectionError("boom")

    response = await client.get(
        "/api/v1/quick/kol-detail",
        params={"platform": "xiaohongshu", "kw_uid": "xhs-1", "nickname": "美食小达人"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["detail"]
    assert body["posts"] == []
    assert body["posts_degraded"] is True
    assert body["points_cost"] == 10  # 热帖调用失败已释放


@pytest.mark.asyncio
async def test_kol_detail_second_call_insufficient_balance_returns_409(
    quick_client_factory, db_session
) -> None:
    client, user, transport, _model = await quick_client_factory(
        balance=15, decisions=[_detail_decision(), _posts_decision()]
    )
    transport.results["kol_detail"] = DETAIL_RESULT
    transport.results["query_raw_posts"] = POSTS_RESULT

    response = await client.get(
        "/api/v1/quick/kol-detail",
        params={"platform": "xiaohongshu", "kw_uid": "xhs-1", "nickname": "美食小达人"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "INSUFFICIENT_POINTS"
    wallet = await db_session.get(Wallet, user.id)
    assert wallet.balance == 5  # kol_detail 已结算，raw.posts 预留失败
    rows = list(
        (await db_session.scalars(select(QuickMcpCall).where(QuickMcpCall.user_id == user.id))).all()
    )
    assert len(rows) == 1
    assert rows[0].internal_tool_name == _DETAIL_TOOL
    assert rows[0].status == "succeeded"


@pytest.mark.asyncio
async def test_kol_detail_rejects_unknown_platform(quick_client_factory) -> None:
    client, _user, _transport, _model = await quick_client_factory()
    response = await client.get(
        "/api/v1/quick/kol-detail",
        params={"platform": "tiktok", "kw_uid": "x", "nickname": "y"},
    )
    assert response.status_code == 422
