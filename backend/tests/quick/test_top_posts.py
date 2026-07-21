import pytest

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


@pytest.mark.asyncio
async def test_top_posts_uses_category_tag_when_match_succeeds(quick_client_factory) -> None:
    client, _user, transport = await quick_client_factory()
    transport.results["match_best_tag"] = BEST_TAG_RESULT
    transport.results["query_raw_posts"] = POSTS_RESULT

    response = await client.get(
        "/api/v1/quick/top-posts", params={"platform": "douyin"}
    )

    assert response.status_code == 200
    body = response.json()
    [match_args] = transport.called_arguments("match_best_tag")
    assert match_args == {"tag_type": "品类标签", "tag_names": ["美食"]}
    [posts_args] = transport.called_arguments("query_raw_posts")
    assert posts_args["target_type"] == "tag"
    assert posts_args["tag_type"] == "品类标签"
    assert posts_args["name"] == "美食"
    assert posts_args["datasource"] == ["短视频__抖音"]
    assert posts_args["order_by"] == "互动数"
    assert posts_args["size"] == 10
    [item] = body["items"]
    assert item["title"] == "爆贴一"
    assert item["interact"] == 21000.0
    assert item["platform"] == "douyin"
    assert body["points_cost"] == 20


@pytest.mark.asyncio
async def test_top_posts_falls_back_to_keyword_with_industry_or(
    quick_client_factory,
) -> None:
    client, _user, transport = await quick_client_factory(industries=("美食", "美妆"))
    transport.results["match_best_tag"] = "未找到匹配的标签"
    transport.results["query_raw_posts"] = POSTS_RESULT

    response = await client.get(
        "/api/v1/quick/top-posts", params={"platform": "xiaohongshu"}
    )

    assert response.status_code == 200
    [posts_args] = transport.called_arguments("query_raw_posts")
    assert posts_args["target_type"] == "keyword"
    assert posts_args["name"] == "美食"
    assert posts_args["anys"] == [["美食", "美妆"]]
    assert posts_args["datasource"] == ["小红书"]


@pytest.mark.asyncio
async def test_top_posts_reuses_cached_tag(quick_client_factory) -> None:
    client, _user, transport = await quick_client_factory()
    transport.results["match_best_tag"] = BEST_TAG_RESULT
    transport.results["query_raw_posts"] = POSTS_RESULT

    first = await client.get("/api/v1/quick/top-posts", params={"platform": "douyin"})
    second = await client.get("/api/v1/quick/top-posts", params={"platform": "douyin"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert transport.call_count("match_best_tag") == 1
    assert second.json()["points_cost"] == 10


@pytest.mark.asyncio
async def test_top_posts_rejects_unsupported_platform(quick_client_factory) -> None:
    client, _user, _transport = await quick_client_factory()
    response = await client.get("/api/v1/quick/top-posts", params={"platform": "weibo"})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_top_posts_degrades_to_hot_kols_when_raw_posts_fails(quick_client_factory) -> None:
    from app.mcp_gateway.transport import McpConnectionError

    client, _user, transport = await quick_client_factory(balance=1000)
    transport.results["match_best_tag"] = BEST_TAG_RESULT
    transport.results["query_raw_posts"] = McpConnectionError("boom")
    transport.results["kol_xiaohongshu_search"] = {
        "KOL 列表": [
            {
                "账号ID (kwUid)": "xhs-1",
                "平台": "xiaohongshu",
                "昵称": "热门达人",
                "粉丝数": 120000,
                "平均互动": 3000.0,
            }
        ]
    }

    response = await client.get(
        "/api/v1/quick/top-posts", params={"platform": "xiaohongshu"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["degraded"] is True
    assert body["items"] == []
    assert body["fallback_kols"][0]["nickname"] == "热门达人"
    # 降级路径走 KOL 搜索工具的 textContentWord。
    [search_args] = transport.called_arguments("kol_xiaohongshu_search")
    assert search_args["request"]["textContentWord"] == "美食"


@pytest.mark.asyncio
async def test_top_posts_502_when_fallback_also_fails(quick_client_factory) -> None:
    from app.mcp_gateway.transport import McpConnectionError

    client, _user, transport = await quick_client_factory(balance=1000)
    transport.results["match_best_tag"] = BEST_TAG_RESULT
    transport.results["query_raw_posts"] = McpConnectionError("boom")
    transport.results["kol_xiaohongshu_search"] = McpConnectionError("boom2")

    response = await client.get(
        "/api/v1/quick/top-posts", params={"platform": "xiaohongshu"}
    )

    assert response.status_code == 502
