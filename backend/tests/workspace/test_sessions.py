import pytest


@pytest.mark.asyncio
async def test_session_history_is_owned_and_restorable(auth_client_factory) -> None:
    alice = await auth_client_factory("13800000001")
    bob = await auth_client_factory("13800000002")

    created = await alice.post(
        "/api/v1/sessions",
        json={
            "brand": "示例品牌",
            "campaign_name": "夏季防晒选人",
            "platforms": ["xiaohongshu", "douyin"],
            "category": "美妆护肤",
            "target_audience": "18-30 岁一二线女性",
            "budget_min": "30000.00",
            "budget_max": "80000.00",
            "initial_query": "寻找兼顾成分科普和转化的达人",
        },
    )
    assert created.status_code == 201
    session_id = created.json()["id"]

    message = await alice.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={"content": "互动率至少 4%，排除近期负面达人"},
    )
    assert message.status_code == 201

    restored = await alice.get(f"/api/v1/sessions/{session_id}")
    assert [item["content"] for item in restored.json()["messages"]] == [
        "寻找兼顾成分科普和转化的达人",
        "互动率至少 4%，排除近期负面达人",
    ]
    assert (await bob.get(f"/api/v1/sessions/{session_id}")).status_code == 404


@pytest.mark.asyncio
async def test_session_can_be_created_without_a_campaign_name(auth_client_factory) -> None:
    client = await auth_client_factory("13800000005")

    created = await client.post(
        "/api/v1/sessions",
        json={
            "brand": "示例品牌",
            "campaign_name": None,
            "platforms": ["xiaohongshu"],
            "category": "美妆护肤",
            "target_audience": "18-30 岁女性",
            "initial_query": "寻找高互动达人",
        },
    )

    assert created.status_code == 201
    assert created.json()["campaign_name"] is None


@pytest.mark.asyncio
async def test_list_patch_and_star_only_affect_owner(auth_client_factory) -> None:
    owner = await auth_client_factory("13700000003")
    outsider = await auth_client_factory("13700000004")
    created = await owner.post(
        "/api/v1/sessions",
        json={
            "brand": "品牌 A",
            "campaign_name": "新品选人",
            "platforms": ["bilibili"],
            "category": "数码",
            "target_audience": "科技兴趣用户",
            "initial_query": "寻找测评达人",
        },
    )
    session_id = created.json()["id"]

    patched = await owner.patch(
        f"/api/v1/sessions/{session_id}",
        json={"title": "重点候选", "is_starred": True},
    )
    owner_list = await owner.get("/api/v1/sessions")

    assert patched.json()["title"] == "重点候选"
    assert patched.json()["is_starred"] is True
    assert [item["id"] for item in owner_list.json()] == [session_id]
    assert (await outsider.get("/api/v1/sessions")).json() == []
    assert (
        await outsider.patch(
            f"/api/v1/sessions/{session_id}", json={"title": "越权修改"}
        )
    ).status_code == 404
