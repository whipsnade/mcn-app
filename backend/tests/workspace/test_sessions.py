from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.selection.models import SessionKolSelection
from app.workspace import service as workspace_service
from app.workspace.models import WorkspaceSession


def _session_payload(**overrides):
    payload = {
        "brand": "示例品牌",
        "campaign_name": "夏季项目",
        "platforms": ["xiaohongshu"],
        "category": "美妆",
        "target_audience": "年轻女性",
        "initial_query": "寻找高互动达人",
    }
    payload.update(overrides)
    return payload


def test_default_session_title_returns_none_when_form_is_empty() -> None:
    title_builder = getattr(workspace_service, "default_session_title", None)

    assert callable(title_builder)
    assert title_builder("", None, "") is None


@pytest.mark.asyncio
async def test_blank_session_has_numbered_default_title_and_no_task(auth_client_factory) -> None:
    client = await auth_client_factory("13600000071")

    first = await client.post("/api/v1/sessions", json={})
    second = await client.post("/api/v1/sessions", json={})

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["title"] == "新会话1"
    assert second.json()["title"] == "新会话2"
    assert first.json()["messages"] == []
    assert first.json()["latest_task"] is None
    assert first.json()["brand"] == ""
    assert first.json()["category"] is None


@pytest.mark.asyncio
async def test_session_update_accepts_filters(auth_client_factory) -> None:
    client = await auth_client_factory("13600000072")
    created = await client.post("/api/v1/sessions", json={})
    session_id = created.json()["id"]

    patched = await client.patch(
        f"/api/v1/sessions/{session_id}",
        json={"filters": {"brainstorm_profile": {"brand": "欧诗漫"}}},
    )

    assert patched.status_code == 200
    assert patched.json()["filters"]["brainstorm_profile"]["brand"] == "欧诗漫"


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
async def test_session_requires_only_industry_and_initial_query(auth_client_factory) -> None:
    client = await auth_client_factory("13800000007")

    created = await client.post(
        "/api/v1/sessions",
        json={
            "brand": "",
            "campaign_name": None,
            "platforms": [],
            "category": "美妆",
            "target_audience": "",
            "initial_query": "筛选最近活跃的达人",
            "filters": {"kol_name": "达人A"},
        },
    )

    assert created.status_code == 201
    assert created.json()["filters"]["kol_name"] == "达人A"


@pytest.mark.asyncio
async def test_session_creation_starts_analysis_for_the_initial_query(auth_client_factory) -> None:
    client = await auth_client_factory("13800000006")

    created = await client.post(
        "/api/v1/sessions",
        json={
            "brand": "分析品牌",
            "campaign_name": None,
            "platforms": ["xiaohongshu"],
            "category": "护肤",
            "target_audience": "25~30 岁女性",
            "initial_query": "筛选最近 30 天互动最高的达人",
        },
    )

    assert created.status_code == 201
    body = created.json()
    assert body["latest_task"]["status"] == "pending"
    assert [message["content"] for message in body["messages"]] == [
        "筛选最近 30 天互动最高的达人"
    ]


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


@pytest.mark.asyncio
async def test_delete_session_is_idempotent_and_hides_all_workspace_routes(
    auth_client_factory,
) -> None:
    owner = await auth_client_factory("13700000011")
    outsider = await auth_client_factory("13700000012")
    created = await owner.post("/api/v1/sessions", json=_session_payload())
    session_id = created.json()["id"]

    assert (await outsider.delete(f"/api/v1/sessions/{session_id}")).status_code == 404
    assert (await owner.delete(f"/api/v1/sessions/{session_id}")).status_code == 204
    assert (await owner.delete(f"/api/v1/sessions/{session_id}")).status_code == 204

    assert (await owner.get("/api/v1/sessions")).json() == []
    assert (await owner.get(f"/api/v1/sessions/{session_id}")).status_code == 404
    assert (
        await owner.patch(f"/api/v1/sessions/{session_id}", json={"title": "不可见"})
    ).status_code == 404
    assert (
        await owner.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={"content": "删除后不可追加"},
        )
    ).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phone", "brand", "campaign_name", "category", "expected_title"),
    [
        ("13600000051", "品牌甲", "项目乙", "美妆", "品牌甲-项目乙"),
        ("13600000052", "品牌甲", None, "美妆", "品牌甲"),
        ("13600000053", "", "项目乙", "美妆", "项目乙"),
        ("13600000054", "", None, "美妆", "美妆 KOL 分析"),
    ],
)
async def test_default_session_title_is_stable_and_not_derived_from_query(
    auth_client_factory, phone, brand, campaign_name, category, expected_title
) -> None:
    client = await auth_client_factory(phone)

    first = await client.post(
        "/api/v1/sessions",
        json=_session_payload(
            brand=brand,
            campaign_name=campaign_name,
            category=category,
            initial_query="第一条完全不同的任务内容",
        ),
    )
    assert first.status_code == 201
    assert first.json()["title"] == expected_title


def _selection_row(user_id: str, session_id: str, uid: str, total: float) -> SessionKolSelection:
    now = datetime.now(UTC).replace(tzinfo=None)
    return SessionKolSelection(
        id=str(uuid4()),
        user_id=user_id,
        session_id=session_id,
        platform="xiaohongshu",
        kol_uid=uid,
        nickname=f"达人{uid}",
        followers=1000,
        city=None,
        profile_url=None,
        fields_json={"export_fields": {}},
        score_json={"total": total, "rating": "推荐", "stars": "★★★★", "dimensions": {}},
        source_tool="tool",
        first_task_id="t1",
        last_task_id="t1",
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_session_read_includes_kol_selection_count(auth_client_factory, db_session) -> None:
    client = await auth_client_factory("13600000082")
    created = await client.post("/api/v1/sessions", json={})
    session_id = created.json()["id"]
    assert created.json()["kol_selection_count"] == 0

    session = await db_session.get(WorkspaceSession, session_id)
    db_session.add(_selection_row(session.user_id, session_id, "a", 80.0))
    db_session.add(_selection_row(session.user_id, session_id, "b", 60.0))
    await db_session.flush()

    detail = await client.get(f"/api/v1/sessions/{session_id}")
    listed = await client.get("/api/v1/sessions")

    assert detail.status_code == 200
    assert detail.json()["kol_selection_count"] == 2
    assert listed.json()[0]["kol_selection_count"] == 2
