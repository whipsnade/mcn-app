from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.selection.models import SessionKolSelection
from app.selection.scoring import rating
from app.selection.service import KolSelectionService
from app.workspace.models import WorkspaceSession


_XHS_TOOL = "datatap.xiaohongshu.kol.search.v1"
_WEIBO_TOOL = "datatap.social.grow.kol.weibo.search.v1"
_NON_KOL_TOOL = "datatap.insight.query.analysis.v1"


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


async def _create_session(db_session, user_factory) -> tuple[str, str]:
    user = await user_factory()
    now = datetime.now(UTC).replace(tzinfo=None)
    session = WorkspaceSession(
        id=str(uuid4()),
        user_id=user.id,
        title="圈选会话",
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


async def _rows(db_session, session_id: str) -> list[SessionKolSelection]:
    return list(
        (
            await db_session.scalars(
                select(SessionKolSelection).where(SessionKolSelection.session_id == session_id)
            )
        ).all()
    )


@pytest.mark.asyncio
async def test_ingest_xiaohongshu_search_upserts_scored_rows(db_session, user_factory) -> None:
    user_id, session_id = await _create_session(db_session, user_factory)
    service = KolSelectionService(db_session)

    written = await service.ingest_tool_evidence(
        user_id=user_id,
        session_id=session_id,
        task_id="task-1",
        tool_name=_XHS_TOOL,
        structured_content=_xhs_payload(_xhs_row("xhs-001"), _xhs_row("xhs-002", 昵称="另一位")),
    )

    assert written == 2
    rows = {row.kol_uid: row for row in await _rows(db_session, session_id)}
    assert set(rows) == {"xhs-001", "xhs-002"}
    row = rows["xhs-001"]
    assert row.user_id == user_id
    assert row.platform == "xiaohongshu"
    assert row.nickname == "美食小探"
    assert row.followers == 125000
    assert row.city == "杭州市"
    assert row.profile_url == "xhs-page-001"
    assert row.source_tool == _XHS_TOOL
    assert row.first_task_id == "task-1"
    assert row.last_task_id == "task-1"
    # 评分：balanced 权重，缺失维度不计分。
    assert row.score_json["total"] == pytest.approx(88 * 0.20 + 65 * 0.25 + 5.2 * 0.20)
    assert row.score_json["data_completeness"] == 65
    expected_rating, expected_stars = rating(row.score_json["total"])
    assert row.score_json["rating"] == expected_rating
    assert row.score_json["stars"] == expected_stars
    assert row.fields_json["quoted_price_cny"] == 8000.0
    assert row.fields_json["export_fields"]["city"] == "杭州市"


@pytest.mark.asyncio
async def test_ingest_merges_repeated_kol_without_clobbering(db_session, user_factory) -> None:
    user_id, session_id = await _create_session(db_session, user_factory)
    service = KolSelectionService(db_session)

    first = await service.ingest_tool_evidence(
        user_id=user_id,
        session_id=session_id,
        task_id="task-1",
        tool_name=_XHS_TOOL,
        structured_content=_xhs_payload(_xhs_row("xhs-001", 综合评分=None)),
    )
    assert first == 1

    # 第二次：昵称/粉丝数/城市缺省（不得冲掉旧值），补上了综合评分。
    second = await service.ingest_tool_evidence(
        user_id=user_id,
        session_id=session_id,
        task_id="task-2",
        tool_name=_XHS_TOOL,
        structured_content=_xhs_payload(
            _xhs_row("xhs-001", 昵称=None, 粉丝数=None, 城市=None, 综合评分=90)
        ),
    )
    assert second == 1

    rows = await _rows(db_session, session_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.nickname == "美食小探"
    assert row.followers == 125000
    assert row.city == "杭州市"
    assert row.first_task_id == "task-1"
    assert row.last_task_id == "task-2"
    assert row.fields_json["content_score"] == 90.0
    assert row.fields_json["followers"] == 125000
    # 合并后重算：content 维度进入评分。
    assert row.score_json["dimensions"]["content"]["raw_score"] == 90.0


@pytest.mark.asyncio
async def test_ingest_skips_non_kol_tools(db_session, user_factory) -> None:
    user_id, session_id = await _create_session(db_session, user_factory)
    service = KolSelectionService(db_session)

    # 品牌统计工具：已登记的非 KOL 工具，直接跳过。
    skipped = await service.ingest_tool_evidence(
        user_id=user_id,
        session_id=session_id,
        task_id="task-1",
        tool_name=_NON_KOL_TOOL,
        structured_content={"result": json.dumps({"品牌声量": 100})},
    )
    # 完全未知的工具名：UnknownEvidenceToolError 同样按 0 处理。
    unknown = await service.ingest_tool_evidence(
        user_id=user_id,
        session_id=session_id,
        task_id="task-1",
        tool_name="datatap.no.such.tool",
        structured_content={"result": "{}"},
    )

    assert skipped == 0
    assert unknown == 0
    assert await _rows(db_session, session_id) == []


@pytest.mark.asyncio
async def test_ingest_bad_row_does_not_block_other_rows(db_session, user_factory) -> None:
    user_id, session_id = await _create_session(db_session, user_factory)
    service = KolSelectionService(db_session)

    written = await service.ingest_tool_evidence(
        user_id=user_id,
        session_id=session_id,
        task_id="task-1",
        tool_name=_WEIBO_TOOL,
        structured_content={
            "result": json.dumps(
                {
                    "KOL 列表": [
                        {"粉丝数": 100},  # 无稳定身份，单行解析失败
                        {"账号ID": "wb-1", "昵称": "微博达人", "粉丝数": 5000},
                    ]
                },
                ensure_ascii=False,
            )
        },
    )

    assert written == 1
    rows = await _rows(db_session, session_id)
    assert len(rows) == 1
    assert rows[0].platform == "weibo"
    assert rows[0].kol_uid == "wb-1"


@pytest.mark.asyncio
async def test_list_selection_orders_by_score_and_checks_ownership(
    db_session, user_factory
) -> None:
    user_id, session_id = await _create_session(db_session, user_factory)
    service = KolSelectionService(db_session)
    await service.ingest_tool_evidence(
        user_id=user_id,
        session_id=session_id,
        task_id="task-1",
        tool_name=_XHS_TOOL,
        structured_content=_xhs_payload(
            _xhs_row("xhs-low", 综合评分=10, 有效粉丝率="10%", **{"互动率-图文笔记": "1%"}),
            _xhs_row("xhs-high", 综合评分=95, 有效粉丝率="95%", **{"互动率-图文笔记": "9%"}),
        ),
    )

    total, rows = await service.list_selection(user_id=user_id, session_id=session_id)

    assert total == 2
    assert [row.kol_uid for row in rows] == ["xhs-high", "xhs-low"]
    assert await service.count_selection(session_id=session_id) == 2
    exported = await service.get_all_for_export(user_id=user_id, session_id=session_id)
    assert [row.kol_uid for row in exported] == ["xhs-high", "xhs-low"]

    other_user = await user_factory()
    with pytest.raises(LookupError, match="session_not_found"):
        await service.list_selection(user_id=other_user.id, session_id=session_id)
    with pytest.raises(LookupError, match="session_not_found"):
        await service.get_all_for_export(user_id=other_user.id, session_id=session_id)


@pytest.mark.asyncio
async def test_count_selections_batches_multiple_sessions(db_session, user_factory) -> None:
    user_id, session_id = await _create_session(db_session, user_factory)
    _other_user_id, empty_session_id = await _create_session(db_session, user_factory)
    service = KolSelectionService(db_session)
    await service.ingest_tool_evidence(
        user_id=user_id,
        session_id=session_id,
        task_id="task-1",
        tool_name=_XHS_TOOL,
        structured_content=_xhs_payload(_xhs_row("xhs-001"), _xhs_row("xhs-002")),
    )

    counts = await service.count_selections(
        session_ids=[session_id, empty_session_id, "missing-session"]
    )

    assert counts == {session_id: 2, empty_session_id: 0, "missing-session": 0}
    assert await service.count_selections(session_ids=[]) == {}
