import pytest

from app.mcp_gateway.transport import McpConnectionError
from app.quick.agent import QUICK_AGENT_MAX_ROUNDS

from .conftest import BEST_TAG_RESULT


POSTS_RESULT = {
    "帖子列表": [
        {
            "标题": "爆贴一",
            "用户昵称": "作者A",
            "互动数": 21000,
            "点赞数": 18000,
            "评论数": 2000,
            "收藏数": 1000,
            "发布时间": "2026-07-15 08:00:00",
            "帖子链接": "https://example.com/top/1",
        }
    ]
}

_RAW_POSTS_TOOL = "datatap.insight.query.raw.posts.v1"


def _raw_posts_decision(**overrides) -> dict:
    arguments = {
        "target_type": "tag",
        "tag_type": "品类标签",
        "name": "美食",
        "datasource": ["短视频__抖音"],
        "start_time": "2026-06-21",
        "end_time": "2026-07-21",
        "order_by": "互动数",
        "size": 10,
    }
    arguments.update(overrides)
    return {
        "action": "call_tool",
        "internal_tool_name": _RAW_POSTS_TOOL,
        "arguments": arguments,
    }


@pytest.mark.asyncio
async def test_top_posts_model_driven_flow(quick_client_factory) -> None:
    client, _user, transport, _model = await quick_client_factory(
        decisions=[
            _raw_posts_decision(),
            {"action": "finish", "result": POSTS_RESULT["帖子列表"]},
        ]
    )
    transport.results["query_raw_posts"] = POSTS_RESULT

    response = await client.get(
        "/api/v1/quick/top-posts", params={"platform": "douyin"}
    )

    assert response.status_code == 200
    body = response.json()
    # 工具按模型选择执行（参数原样透传到传输层）。
    [posts_args] = transport.called_arguments("query_raw_posts")
    assert posts_args["target_type"] == "tag"
    assert posts_args["name"] == "美食"
    assert posts_args["datasource"] == ["短视频__抖音"]
    [item] = body["items"]
    assert item["title"] == "爆贴一"
    assert item["interact"] == 21000.0
    assert item["platform"] == "douyin"
    assert body["points_cost"] == 10
    assert body["degraded"] is False
    assert body["fallback_kols"] == []


@pytest.mark.asyncio
async def test_top_posts_model_may_call_tag_match_first(quick_client_factory) -> None:
    client, _user, transport, _model = await quick_client_factory(
        decisions=[
            {
                "action": "call_tool",
                "internal_tool_name": "datatap.insight.match.best.tag.v1",
                "arguments": {"tag_type": "品类标签", "tag_names": ["美食"]},
            },
            _raw_posts_decision(),
            {"action": "finish", "result": POSTS_RESULT["帖子列表"]},
        ]
    )
    transport.results["match_best_tag"] = BEST_TAG_RESULT
    transport.results["query_raw_posts"] = POSTS_RESULT

    response = await client.get(
        "/api/v1/quick/top-posts", params={"platform": "douyin"}
    )

    assert response.status_code == 200
    assert transport.call_count("match_best_tag") == 1
    assert response.json()["points_cost"] == 20


@pytest.mark.asyncio
async def test_top_posts_tool_failure_fed_back_model_finishes_empty(
    quick_client_factory,
) -> None:
    client, _user, transport, _model = await quick_client_factory(
        decisions=[
            _raw_posts_decision(),
            {"action": "finish", "result": []},
        ]
    )
    transport.results["query_raw_posts"] = McpConnectionError("boom")

    response = await client.get(
        "/api/v1/quick/top-posts", params={"platform": "douyin"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["points_cost"] == 0  # 失败调用已释放，不计费


@pytest.mark.asyncio
async def test_top_posts_502_when_model_keeps_deciding_invalid(quick_client_factory) -> None:
    client, _user, _transport, _model = await quick_client_factory(
        decisions=[
            {"action": "call_tool", "internal_tool_name": "datatap.not.allowed.v1", "arguments": {}},
            {"action": "call_tool", "internal_tool_name": "datatap.not.allowed.v1", "arguments": {}},
        ]
    )

    response = await client.get(
        "/api/v1/quick/top-posts", params={"platform": "douyin"}
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "QUICK_CALL_FAILED"


@pytest.mark.asyncio
async def test_top_posts_502_when_model_never_finishes(quick_client_factory) -> None:
    client, _user, transport, _model = await quick_client_factory(
        decisions=[_raw_posts_decision()] * (QUICK_AGENT_MAX_ROUNDS + 1)
    )
    transport.results["query_raw_posts"] = POSTS_RESULT

    response = await client.get(
        "/api/v1/quick/top-posts", params={"platform": "douyin"}
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "QUICK_CALL_FAILED"


@pytest.mark.asyncio
async def test_top_posts_rejects_unsupported_platform(quick_client_factory) -> None:
    client, _user, _transport, _model = await quick_client_factory()
    response = await client.get("/api/v1/quick/top-posts", params={"platform": "weibo"})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_top_posts_insufficient_balance_returns_409(
    quick_client_factory, db_session
) -> None:
    from app.billing.models import Wallet

    client, user, transport, _model = await quick_client_factory(
        balance=5, decisions=[_raw_posts_decision()]
    )
    transport.results["query_raw_posts"] = POSTS_RESULT

    response = await client.get(
        "/api/v1/quick/top-posts", params={"platform": "douyin"}
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "INSUFFICIENT_POINTS"
    wallet = await db_session.get(Wallet, user.id)
    assert wallet.balance == 5
    assert wallet.reserved == 0
