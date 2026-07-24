from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.selection.models import KolSelectionItem, SessionKolSelection
from app.selection.service import KolSelectionService, serialize_selection_item
from app.workspace.models import WorkspaceSession


_XHS_TOOL = "datatap.xiaohongshu.kol.search.v1"
_INSIGHT_QUERY_TOOL = "datatap.insight.query.analysis.v1"


def _xhs_payload(*rows: dict[str, Any]) -> dict[str, Any]:
    return {"result": json.dumps({"KOL 列表": list(rows)}, ensure_ascii=False)}


def _xhs_row(uid: str, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "账号ID (kwUid)": uid,
        "昵称": "美食小探",
        "粉丝数": "12.5万",
        "互动率-图文笔记": "5.2%",
        "综合评分": 88,
        "有效粉丝率": "65%",
        "城市": "杭州市",
        "预估报价-图文": "¥8,000",
        "主页": "xhs-page-001",
    }
    row.update(overrides)
    return row


def _insight_user_row(uid: str | None, nickname: str, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "用户昵称": nickname,
        "声量": 1,
        "互动数": 9,
        "点赞数": 2,
        "评论数": 2,
        "收藏数": 4,
        "用户粉丝数": 100,
    }
    if uid is not None:
        row["用户ID"] = uid
    row.update(overrides)
    return row


async def _create_session(db_session, user_factory) -> tuple[str, str]:
    user = await user_factory()
    now = datetime.now(UTC).replace(tzinfo=None)
    session = WorkspaceSession(
        id=str(uuid4()),
        user_id=user.id,
        title="set 化测试会话",
        brand="",
        campaign_name=None,
        status="active",
        platforms=["xiaohongshu"],
        category="美食",
        target_audience="",
        budget_min=None,
        budget_max=None,
        filters_snapshot={},
        is_starred=False,
        last_accessed_at=now,
        created_at=now,
        updated_at=now,
    )
    db_session.add(session)
    await db_session.flush()
    return user.id, session.id


async def _set_items(db_session, selection_set_id: str) -> list[KolSelectionItem]:
    return list(
        (
            await db_session.scalars(
                select(KolSelectionItem).where(
                    KolSelectionItem.selection_set_id == selection_set_id
                )
            )
        ).all()
    )


@pytest.mark.asyncio
async def test_ensure_selection_set_creates_then_reuses_by_task_and_goal(
    db_session, user_factory
) -> None:
    user_id, session_id = await _create_session(db_session, user_factory)
    service = KolSelectionService(db_session)

    first = await service.ensure_selection_set(
        user_id,
        session_id,
        task_id="task-1",
        goal_id="goal-1",
        title="默认名单",
        scope={"brand": "测试品牌"},
    )
    assert first.version == 1
    assert first.title == "默认名单"
    assert first.scope_json == {"brand": "测试品牌"}
    assert first.status == "active"
    assert first.task_id == "task-1"
    assert first.goal_id == "goal-1"

    # 同 task_id / goal_id 复用，不新建。
    second = await service.ensure_selection_set(
        user_id, session_id, task_id="task-1", goal_id="goal-1", title="默认名单"
    )
    assert second.id == first.id
    by_goal_only = await service.ensure_selection_set(
        user_id, session_id, goal_id="goal-1", title="默认名单"
    )
    assert by_goal_only.id == first.id


@pytest.mark.asyncio
async def test_ensure_selection_set_increments_version_per_session(
    db_session, user_factory
) -> None:
    user_id, session_id = await _create_session(db_session, user_factory)
    service = KolSelectionService(db_session)

    first = await service.ensure_selection_set(user_id, session_id, task_id="task-1", title="名单")
    second = await service.ensure_selection_set(user_id, session_id, task_id="task-2", title="名单")

    assert (first.version, second.version) == (1, 2)
    assert second.id != first.id


@pytest.mark.asyncio
async def test_ensure_selection_set_rejects_foreign_session(db_session, user_factory) -> None:
    _, session_id = await _create_session(db_session, user_factory)
    other = await user_factory()
    service = KolSelectionService(db_session)

    with pytest.raises(LookupError, match="session_not_found"):
        await service.ensure_selection_set(other.id, session_id, title="名单")


@pytest.mark.asyncio
async def test_ingest_tool_evidence_to_set_matches_legacy_ingest(db_session, user_factory) -> None:
    user_id, session_id = await _create_session(db_session, user_factory)
    service = KolSelectionService(db_session)
    selection_set = await service.ensure_selection_set(
        user_id, session_id, task_id="task-1", title="默认名单"
    )
    payload = _xhs_payload(_xhs_row("xhs-001"), _xhs_row("xhs-002", 昵称="另一位"))

    written_set = await service.ingest_tool_evidence_to_set(
        user_id=user_id,
        selection_set_id=selection_set.id,
        task_id="task-1",
        tool_name=_XHS_TOOL,
        structured_content=payload,
    )
    written_legacy = await service.ingest_tool_evidence(
        user_id=user_id,
        session_id=session_id,
        task_id="task-1",
        tool_name=_XHS_TOOL,
        structured_content=payload,
    )

    assert written_set == written_legacy == 2
    items = {item.kol_uid: item for item in await _set_items(db_session, selection_set.id)}
    legacy_rows = {
        row.kol_uid: row
        for row in (
            await db_session.scalars(
                select(SessionKolSelection).where(
                    SessionKolSelection.session_id == session_id
                )
            )
        ).all()
    }
    assert set(items) == set(legacy_rows) == {"xhs-001", "xhs-002"}
    for kol_uid, item in items.items():
        legacy = legacy_rows[kol_uid]
        assert item.user_id == legacy.user_id == user_id
        assert item.platform == legacy.platform
        assert item.nickname == legacy.nickname
        assert item.followers == legacy.followers
        assert item.city == legacy.city
        assert item.profile_url == legacy.profile_url
        assert item.source_tool == legacy.source_tool
        assert item.first_task_id == legacy.first_task_id
        assert item.score_json["total"] == pytest.approx(legacy.score_json["total"])


@pytest.mark.asyncio
async def test_ingest_to_set_merges_derived_uid_into_real_uid(db_session, user_factory) -> None:
    """派生 uid（无 用户ID 证据）→ 真实 uid 二次归并在 items 表同样生效。"""
    user_id, session_id = await _create_session(db_session, user_factory)
    service = KolSelectionService(db_session)
    selection_set = await service.ensure_selection_set(
        user_id, session_id, task_id="task-1", title="默认名单"
    )

    first = await service.ingest_tool_evidence_to_set(
        user_id=user_id,
        selection_set_id=selection_set.id,
        task_id="task-1",
        tool_name=_INSIGHT_QUERY_TOOL,
        structured_content={
            "result": json.dumps(
                [_insight_user_row(None, "uan"), _insight_user_row(None, "逃离地球世界")],
                ensure_ascii=False,
            )
        },
        arguments={"datasource": ["小红书"]},
    )
    assert first == 2
    assert all(
        item.kol_uid.startswith("xiaohongshu:")
        for item in await _set_items(db_session, selection_set.id)
    )

    second = await service.ingest_tool_evidence_to_set(
        user_id=user_id,
        selection_set_id=selection_set.id,
        task_id="task-1",
        tool_name=_INSIGHT_QUERY_TOOL,
        structured_content={
            "result": json.dumps(
                [_insight_user_row("uid-uan", "uan"), _insight_user_row("uid-earth", "逃离地球世界")],
                ensure_ascii=False,
            )
        },
        arguments={"datasource": ["小红书"]},
    )
    assert second == 2

    items = {item.kol_uid: item for item in await _set_items(db_session, selection_set.id)}
    # 派生占位行已归并，只剩真实 uid 两行。
    assert set(items) == {"uid-uan", "uid-earth"}
    assert items["uid-uan"].nickname == "uan"


@pytest.mark.asyncio
async def test_latest_selection_set_returns_highest_version(db_session, user_factory) -> None:
    user_id, session_id = await _create_session(db_session, user_factory)
    service = KolSelectionService(db_session)

    assert await service.latest_selection_set(session_id) is None
    await service.ensure_selection_set(user_id, session_id, task_id="task-1", title="名单")
    latest = await service.ensure_selection_set(user_id, session_id, task_id="task-2", title="名单")

    found = await service.latest_selection_set(session_id)
    assert found is not None
    assert found.id == latest.id
    assert found.version == 2


@pytest.mark.asyncio
async def test_list_count_export_items_and_dto_shape(db_session, user_factory) -> None:
    user_id, session_id = await _create_session(db_session, user_factory)
    service = KolSelectionService(db_session)
    selection_set = await service.ensure_selection_set(
        user_id, session_id, task_id="task-1", title="默认名单"
    )
    await service.ingest_tool_evidence_to_set(
        user_id=user_id,
        selection_set_id=selection_set.id,
        task_id="task-1",
        tool_name=_XHS_TOOL,
        structured_content=_xhs_payload(
            _xhs_row("xhs-high", 综合评分=95), _xhs_row("xhs-low", 综合评分=10, 有效粉丝率="10%")
        ),
    )

    total, page = await service.list_selection_items(
        user_id=user_id, selection_set_id=selection_set.id
    )
    assert total == 2
    # 按总分倒序。
    assert [item.kol_uid for item in page] == ["xhs-high", "xhs-low"]
    assert await service.count_items(selection_set.id) == 2

    _, first_page = await service.list_selection_items(
        user_id=user_id, selection_set_id=selection_set.id, offset=0, limit=1
    )
    assert [item.kol_uid for item in first_page] == ["xhs-high"]

    export_rows = await service.get_all_items_for_export(
        user_id=user_id, selection_set_id=selection_set.id
    )
    assert [item.kol_uid for item in export_rows] == ["xhs-high", "xhs-low"]

    dto = serialize_selection_item(page[0])
    assert set(dto) == {
        "platform",
        "kol_uid",
        "nickname",
        "followers",
        "city",
        "profile_url",
        "fields",
        "score",
    }
    assert dto["kol_uid"] == "xhs-high"


@pytest.mark.asyncio
async def test_item_queries_enforce_ownership(db_session, user_factory) -> None:
    user_id, session_id = await _create_session(db_session, user_factory)
    other = await user_factory()
    service = KolSelectionService(db_session)
    selection_set = await service.ensure_selection_set(
        user_id, session_id, task_id="task-1", title="默认名单"
    )

    with pytest.raises(LookupError):
        await service.list_selection_items(user_id=other.id, selection_set_id=selection_set.id)
    with pytest.raises(LookupError):
        await service.get_all_items_for_export(
            user_id=other.id, selection_set_id=selection_set.id
        )
    with pytest.raises(LookupError):
        await service.ingest_tool_evidence_to_set(
            user_id=other.id,
            selection_set_id=selection_set.id,
            task_id="task-1",
            tool_name=_XHS_TOOL,
            structured_content=_xhs_payload(_xhs_row("xhs-001")),
        )
    with pytest.raises(LookupError, match="selection_set_not_found"):
        await service.list_selection_items(user_id=user_id, selection_set_id="missing")
