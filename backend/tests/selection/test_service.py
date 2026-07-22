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
_NON_KOL_TOOL = "datatap.insight.social.statistic.trend.v1"
_DETAIL_TOOL = "datatap.social.grow.kol.detail.v1"
_INSIGHT_QUERY_TOOL = "datatap.insight.query.analysis.v1"
_INSIGHT_HOT_USER_TOOL = "datatap.insight.social.statistic.hot.user.v1"
_DETAIL_UID = "60064140000000000100178b"


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


def _detail_payload(**row_overrides: Any) -> dict[str, Any]:
    """kol.detail 生产形态：单行 detail，平台只在调用参数里，行内没有。"""
    row: dict[str, Any] = {
        "账号ID (kwUid)": _DETAIL_UID,
        "发帖数据-汇总统计": {"作品数": 42, "平均互动": 900, "平均阅读": 30000},
        "受众画像": {
            "粉丝年龄分布": [{"键": "18-24", "值": 0.4}, {"键": "25-34", "值": 0.35}],
            "粉丝省份分布Top10": [{"键": "浙江", "值": 0.3}, {"键": "江苏", "值": 0.2}],
            "粉丝兴趣分布": [{"键": "美食", "值": 0.5}, {"键": "旅行", "值": 0.2}],
        },
        "商业表现-蒲公英商单": {"汇总统计": {"合作笔记数": 3, "平均互动": 1200}},
        "商业表现-品牌提及": {"汇总统计": {"提及品牌数": 2}},
    }
    row.update(row_overrides)
    return {"result": json.dumps(row, ensure_ascii=False)}


def _insight_user_row(uid: str | None, nickname: str, **overrides: Any) -> dict[str, Any]:
    """query.analysis 按用户维度统计的生产行形态。"""
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


@pytest.mark.asyncio
async def test_ingest_kol_detail_uses_platform_from_arguments(db_session, user_factory) -> None:
    """A1：kol.detail 行内无平台字段，平台由 arguments.platform 注入。"""
    user_id, session_id = await _create_session(db_session, user_factory)
    service = KolSelectionService(db_session)

    # 无 arguments：平台身份无法建立（生产 bug 现象），不写行也不报错。
    without_hint = await service.ingest_tool_evidence(
        user_id=user_id,
        session_id=session_id,
        task_id="task-1",
        tool_name=_DETAIL_TOOL,
        structured_content=_detail_payload(),
    )
    assert without_hint == 0

    written = await service.ingest_tool_evidence(
        user_id=user_id,
        session_id=session_id,
        task_id="task-1",
        tool_name=_DETAIL_TOOL,
        structured_content=_detail_payload(),
        arguments={"kwUidList": [_DETAIL_UID], "platform": "xiaohongshu"},
    )

    assert written == 1
    rows = await _rows(db_session, session_id)
    assert len(rows) == 1
    assert rows[0].platform == "xiaohongshu"
    assert rows[0].kol_uid == _DETAIL_UID


@pytest.mark.asyncio
async def test_ingest_insight_user_stats_rows(db_session, user_factory) -> None:
    """A2：query.analysis 按用户维度统计的 per-user 行落圈选名单。"""
    user_id, session_id = await _create_session(db_session, user_factory)
    service = KolSelectionService(db_session)

    written = await service.ingest_tool_evidence(
        user_id=user_id,
        session_id=session_id,
        task_id="task-1",
        tool_name=_INSIGHT_QUERY_TOOL,
        structured_content={
            "result": json.dumps(
                [
                    _insight_user_row("uid-1", "uan"),
                    _insight_user_row("uid-2", "另一达人", 用户粉丝数="1.2万"),
                ],
                ensure_ascii=False,
            )
        },
        arguments={"datasource": ["小红书"], "dimensions": ["用户昵称", "用户ID"]},
    )

    assert written == 2
    rows = {row.kol_uid: row for row in await _rows(db_session, session_id)}
    assert set(rows) == {"uid-1", "uid-2"}
    row = rows["uid-1"]
    assert row.platform == "xiaohongshu"
    assert row.nickname == "uan"
    assert row.followers == 100
    export_fields = row.fields_json["export_fields"]
    assert export_fields["volume"] == 1
    assert export_fields["total_interactions"] == 9
    assert export_fields["total_likes"] == 2
    assert export_fields["total_comments"] == 2
    assert export_fields["total_favorites"] == 4
    assert rows["uid-2"].followers == 12000


@pytest.mark.asyncio
async def test_ingest_insight_user_stats_derives_uid_without_user_id(
    db_session, user_factory
) -> None:
    """A2：hot.user 行缺 用户ID 时按 platform+nickname sha256 派生稳定身份。"""
    user_id, session_id = await _create_session(db_session, user_factory)
    service = KolSelectionService(db_session)

    written = await service.ingest_tool_evidence(
        user_id=user_id,
        session_id=session_id,
        task_id="task-1",
        tool_name=_INSIGHT_HOT_USER_TOOL,
        structured_content={
            "result": json.dumps([_insight_user_row(None, "uan")], ensure_ascii=False)
        },
        arguments={"datasource": ["小红书"]},
    )

    assert written == 1
    rows = await _rows(db_session, session_id)
    assert len(rows) == 1
    # 派生身份：平台前缀 + sha256 截断，确定性可合并。
    assert rows[0].kol_uid.startswith("xiaohongshu:")
    assert len(rows[0].kol_uid) == len("xiaohongshu:") + 24
    assert rows[0].nickname == "uan"


@pytest.mark.asyncio
async def test_ingest_insight_non_user_rows_are_skipped(db_session, user_factory) -> None:
    """A2：大盘统计形态（无 用户昵称）返回 0 行，不报错。"""
    user_id, session_id = await _create_session(db_session, user_factory)
    service = KolSelectionService(db_session)

    written = await service.ingest_tool_evidence(
        user_id=user_id,
        session_id=session_id,
        task_id="task-1",
        tool_name=_INSIGHT_QUERY_TOOL,
        structured_content={
            "result": json.dumps(
                [{"日期": "2026-07-01", "声量": 120}, {"日期": "2026-07-02", "声量": 98}],
                ensure_ascii=False,
            )
        },
        arguments={"datasource": ["小红书"]},
    )

    assert written == 0
    assert await _rows(db_session, session_id) == []


@pytest.mark.asyncio
async def test_ingest_kol_detail_extracts_nested_sections(db_session, user_factory) -> None:
    """A3：detail 行的发帖/受众/商业 section 提取为导出与评分字段。"""
    user_id, session_id = await _create_session(db_session, user_factory)
    service = KolSelectionService(db_session)

    written = await service.ingest_tool_evidence(
        user_id=user_id,
        session_id=session_id,
        task_id="task-1",
        tool_name=_DETAIL_TOOL,
        structured_content=_detail_payload(),
        arguments={"platform": "xiaohongshu"},
    )

    assert written == 1
    (row,) = await _rows(db_session, session_id)
    fields = row.fields_json
    export_fields = fields["export_fields"]
    # 发帖数据-汇总统计 → 导出字段 + 互动率估算（平均互动/平均阅读）。
    assert export_fields["works_count"] == 42
    assert export_fields["average_interactions"] == 900
    assert export_fields["average_reads"] == 30000
    assert fields["engagement_rate"] == pytest.approx(3.0)
    assert fields["engagement_score"] == pytest.approx(3.0)
    # 受众画像 → 导出受众字段 + analytics 受众分布。
    assert export_fields["age_18_24"] == pytest.approx(40)
    assert export_fields["age_25_34"] == pytest.approx(35)
    assert export_fields["province"] == "浙江"
    assert "美食" in export_fields["content_tags"]
    analytics = fields["analytics_fields"]
    assert analytics["audience_age"] == {"18-24": 40, "25-34": 35}
    assert analytics["audience_regions"]["浙江"] == 30
    # 商业表现汇总 → 导出字段。
    assert export_fields["business_order_stats"]["合作笔记数"] == 3
    assert export_fields["brand_mention_stats"]["提及品牌数"] == 2


@pytest.mark.asyncio
async def test_ingest_kol_detail_empty_sections_do_not_clobber(
    db_session, user_factory
) -> None:
    """A3：空 section 不报错，合并时也不冲掉已有值。"""
    user_id, session_id = await _create_session(db_session, user_factory)
    service = KolSelectionService(db_session)

    await service.ingest_tool_evidence(
        user_id=user_id,
        session_id=session_id,
        task_id="task-1",
        tool_name=_DETAIL_TOOL,
        structured_content=_detail_payload(),
        arguments={"platform": "xiaohongshu"},
    )
    written = await service.ingest_tool_evidence(
        user_id=user_id,
        session_id=session_id,
        task_id="task-2",
        tool_name=_DETAIL_TOOL,
        structured_content=_detail_payload(
            **{
                "发帖数据-汇总统计": {},
                "受众画像": {},
                "商业表现-蒲公英商单": {"汇总统计": {}},
                "商业表现-品牌提及": {},
            }
        ),
        arguments={"platform": "xiaohongshu"},
    )

    assert written == 1
    rows = await _rows(db_session, session_id)
    assert len(rows) == 1
    export_fields = rows[0].fields_json["export_fields"]
    assert export_fields["average_interactions"] == 900
    assert export_fields["province"] == "浙江"
    assert export_fields["business_order_stats"]["合作笔记数"] == 3
    assert rows[0].fields_json["engagement_rate"] == pytest.approx(3.0)


@pytest.mark.asyncio
async def test_ingest_query_analysis_then_kol_detail_merges_into_one_row(
    db_session, user_factory
) -> None:
    """端到端：query.analysis 建行（昵称/粉丝）→ kol.detail 按同 uid 合并补字段。"""
    user_id, session_id = await _create_session(db_session, user_factory)
    service = KolSelectionService(db_session)

    first = await service.ingest_tool_evidence(
        user_id=user_id,
        session_id=session_id,
        task_id="task-1",
        tool_name=_INSIGHT_QUERY_TOOL,
        structured_content={
            "result": json.dumps(
                [_insight_user_row(_DETAIL_UID, "uan")], ensure_ascii=False
            )
        },
        arguments={"datasource": ["小红书"]},
    )
    second = await service.ingest_tool_evidence(
        user_id=user_id,
        session_id=session_id,
        task_id="task-1",
        tool_name=_DETAIL_TOOL,
        structured_content=_detail_payload(),
        arguments={"platform": "xiaohongshu"},
    )

    assert (first, second) == (1, 1)
    rows = await _rows(db_session, session_id)
    assert len(rows) == 1
    row = rows[0]
    # 昵称/粉丝数来自 query.analysis 行。
    assert row.nickname == "uan"
    assert row.followers == 100
    # 互动与受众字段来自 kol.detail 合并。
    assert row.fields_json["engagement_rate"] == pytest.approx(3.0)
    export_fields = row.fields_json["export_fields"]
    assert export_fields["total_interactions"] == 9
    assert export_fields["average_interactions"] == 900
    assert export_fields["province"] == "浙江"
    assert row.fields_json["analytics_fields"]["audience_age"] == {"18-24": 40, "25-34": 35}
    assert row.source_tool == _INSIGHT_QUERY_TOOL
    assert row.last_task_id == "task-1"
