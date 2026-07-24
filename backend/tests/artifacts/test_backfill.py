from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.artifacts.backfill import LEGACY_SET_TITLE, run_backfill
from app.artifacts.models import TaskArtifact
from app.reporting.models import AnalysisReport
from app.selection.models import (
    KolSelectionItem,
    KolSelectionSet,
    SessionKolSelection,
)
from app.workspace.models import WorkspaceSession


async def _create_session(db_session, user_factory) -> tuple[str, str]:
    user = await user_factory()
    now = datetime.now(UTC).replace(tzinfo=None)
    session = WorkspaceSession(
        id=str(uuid4()),
        user_id=user.id,
        title="回填测试会话",
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


def _legacy_row(user_id: str, session_id: str, kol_uid: str, **overrides) -> SessionKolSelection:
    created = datetime(2026, 7, 1, 8, 0, 0)
    row = SessionKolSelection(
        id=str(uuid4()),
        user_id=user_id,
        session_id=session_id,
        platform="xiaohongshu",
        kol_uid=kol_uid,
        nickname=f"达人{kol_uid}",
        followers=125000,
        city="杭州市",
        profile_url=f"https://example.com/{kol_uid}",
        fields_json={"quoted_price_cny": 8000.0},
        score_json={"total": 80.0, "rating": "重点推荐", "stars": 5},
        source_tool="datatap.xiaohongshu.kol.search.v1",
        first_task_id="task-1",
        last_task_id="task-2",
        created_at=created,
        updated_at=created,
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


async def _create_report(db_session, session_id: str) -> AnalysisReport:
    now = datetime.now(UTC).replace(tzinfo=None)
    report = AnalysisReport(
        id=str(uuid4()),
        task_id=None,
        session_id=session_id,
        version=1,
        title="旧分析报告",
        blocks_json=[],
        conclusion_text=None,
        status="completed",
        created_at=now,
        updated_at=now,
    )
    db_session.add(report)
    await db_session.flush()
    return report


@pytest.mark.asyncio
async def test_backfill_creates_set_items_and_artifacts(db_session, user_factory) -> None:
    user_id, session_id = await _create_session(db_session, user_factory)
    db_session.add(_legacy_row(user_id, session_id, "uid-001"))
    db_session.add(_legacy_row(user_id, session_id, "uid-002", platform="douyin"))
    report = await _create_report(db_session, session_id)

    stats = await run_backfill(db_session)

    assert stats.selection_sets_created == 1
    assert stats.selection_items_created == 2
    assert stats.selection_items_skipped == 0
    assert stats.report_artifacts_created == 1
    assert stats.set_artifacts_created == 1

    selection_set = (
        await db_session.scalars(
            select(KolSelectionSet).where(KolSelectionSet.session_id == session_id)
        )
    ).one()
    assert selection_set.title == LEGACY_SET_TITLE
    assert selection_set.version == 1
    assert selection_set.status == "completed"
    assert selection_set.task_id is None
    assert selection_set.goal_id is None
    assert selection_set.scope_json is None

    items = {
        item.kol_uid: item
        for item in (
            await db_session.scalars(
                select(KolSelectionItem).where(
                    KolSelectionItem.selection_set_id == selection_set.id
                )
            )
        ).all()
    }
    assert set(items) == {"uid-001", "uid-002"}
    item = items["uid-001"]
    assert item.user_id == user_id
    assert item.platform == "xiaohongshu"
    assert item.nickname == "达人uid-001"
    assert item.followers == 125000
    assert item.city == "杭州市"
    assert item.profile_url == "https://example.com/uid-001"
    assert item.fields_json == {"quoted_price_cny": 8000.0}
    assert item.score_json["rating"] == "重点推荐"
    assert item.source_tool == "datatap.xiaohongshu.kol.search.v1"
    assert item.first_task_id == "task-1"
    assert item.last_task_id == "task-2"
    # 时间戳沿用原行
    assert item.created_at == datetime(2026, 7, 1, 8, 0, 0)

    artifacts = {
        artifact.artifact_key: artifact
        for artifact in (await db_session.scalars(select(TaskArtifact))).all()
    }
    report_artifact = artifacts[f"legacy:{report.id}:kol_report"]
    assert report_artifact.artifact_type == "kol_report"
    assert report_artifact.session_id == session_id
    assert report_artifact.report_id == report.id
    assert report_artifact.title == report.title
    assert report_artifact.version == report.version
    assert report_artifact.status == report.status
    set_artifact = artifacts[f"legacy:{selection_set.id}:kol_selection_set"]
    assert set_artifact.artifact_type == "kol_selection_set"
    assert set_artifact.selection_set_id == selection_set.id
    assert set_artifact.status == "completed"


@pytest.mark.asyncio
async def test_backfill_is_idempotent_on_second_run(db_session, user_factory) -> None:
    user_id, session_id = await _create_session(db_session, user_factory)
    db_session.add(_legacy_row(user_id, session_id, "uid-001"))
    db_session.add(_legacy_row(user_id, session_id, "uid-002"))
    await _create_report(db_session, session_id)

    first = await run_backfill(db_session)
    second = await run_backfill(db_session)

    assert first.selection_sets_created == 1
    assert second.selection_sets_created == 0
    assert second.selection_items_created == 0
    assert second.selection_items_skipped == 2
    assert second.report_artifacts_created == 0
    assert second.report_artifacts_skipped == 1
    assert second.set_artifacts_created == 0
    assert second.set_artifacts_skipped == 1

    async def _count(model) -> int:
        return int(await db_session.scalar(select(func.count()).select_from(model)) or 0)

    assert await _count(KolSelectionSet) == 1
    assert await _count(KolSelectionItem) == 2
    assert await _count(TaskArtifact) == 2
