from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.artifacts.models import ArtifactReadState, TaskArtifact
from app.artifacts.service import ArtifactService, module_key_of
from app.reporting.models import AnalysisReport
from app.workspace.models import WorkspaceSession


async def _create_session(db_session, user_factory) -> tuple[str, str]:
    user = await user_factory()
    now = datetime.now(UTC).replace(tzinfo=None)
    session = WorkspaceSession(
        id=str(uuid4()),
        user_id=user.id,
        title="artifact 测试会话",
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


async def _artifact_count(db_session) -> int:
    return int(await db_session.scalar(select(func.count()).select_from(TaskArtifact)) or 0)


async def _create_report(db_session, session_id: str) -> str:
    now = datetime.now(UTC).replace(tzinfo=None)
    report = AnalysisReport(
        id=str(uuid4()),
        task_id=None,
        session_id=session_id,
        version=1,
        title="分析报告",
        blocks_json=[],
        conclusion_text=None,
        status="completed",
        created_at=now,
        updated_at=now,
    )
    db_session.add(report)
    await db_session.flush()
    return report.id


@pytest.mark.asyncio
async def test_register_artifact_creates_row(db_session, user_factory) -> None:
    user_id, session_id = await _create_session(db_session, user_factory)
    report_id = await _create_report(db_session, session_id)
    service = ArtifactService(db_session)

    artifact = await service.register_artifact(
        user_id=user_id,
        session_id=session_id,
        artifact_key="goal:g1:kol_report",
        artifact_type="kol_report",
        title="KOL 分析报告",
        version=1,
        task_id="task-1",
        report_id=report_id,
        scope={"brand": "海底捞"},
    )

    assert artifact.session_id == session_id
    assert artifact.task_id == "task-1"
    assert artifact.goal_id is None
    assert artifact.artifact_key == "goal:g1:kol_report"
    assert artifact.artifact_type == "kol_report"
    assert artifact.title == "KOL 分析报告"
    assert artifact.version == 1
    assert artifact.status == "completed"
    assert artifact.report_id == report_id
    assert artifact.selection_set_id is None
    assert artifact.scope_json == {"brand": "海底捞"}
    assert await _artifact_count(db_session) == 1


@pytest.mark.asyncio
async def test_register_artifact_is_idempotent_and_updates_fields(
    db_session, user_factory
) -> None:
    user_id, session_id = await _create_session(db_session, user_factory)
    report_id = await _create_report(db_session, session_id)
    service = ArtifactService(db_session)

    first = await service.register_artifact(
        user_id=user_id,
        session_id=session_id,
        artifact_key="goal:g1:kol_report",
        artifact_type="kol_report",
        title="旧标题",
        version=1,
        status="running",
        report_id=report_id,
    )
    second = await service.register_artifact(
        user_id=user_id,
        session_id=session_id,
        artifact_key="goal:g1:kol_report",
        artifact_type="kol_report",
        title="新标题",
        version=2,
        status="completed",
        report_id=report_id,
        scope={"brand": "新品牌"},
    )

    assert second.id == first.id
    assert second.title == "新标题"
    assert second.version == 2
    assert second.status == "completed"
    assert second.report_id == report_id
    assert second.scope_json == {"brand": "新品牌"}
    assert await _artifact_count(db_session) == 1


@pytest.mark.asyncio
async def test_register_artifact_requires_exactly_one_target(
    db_session, user_factory
) -> None:
    user_id, session_id = await _create_session(db_session, user_factory)
    service = ArtifactService(db_session)

    base = {
        "user_id": user_id,
        "session_id": session_id,
        "artifact_key": "k",
        "artifact_type": "kol_report",
        "title": "t",
        "version": 1,
    }
    with pytest.raises(ValueError):
        await service.register_artifact(**base)  # 两者都不传
    with pytest.raises(ValueError):
        await service.register_artifact(
            **base, report_id="r1", selection_set_id="s1"
        )  # 两者都传
    with pytest.raises(ValueError):
        await service.register_artifact(**base, selection_set_id="s1")  # kol_report 缺 report_id
    with pytest.raises(ValueError):
        await service.register_artifact(
            **{**base, "artifact_type": "kol_selection_set"}, report_id="r1"
        )  # kol_selection_set 缺 selection_set_id


def test_module_key_of_mapping() -> None:
    assert module_key_of("kol_report") == "kol_analysis"
    assert module_key_of("kol_selection_set") == "kol_selection"
    assert module_key_of("brand_report") == "brand"
    assert module_key_of("campaign_report") == "campaign"
    with pytest.raises(ValueError):
        module_key_of("unknown_type")


@pytest.mark.asyncio
async def test_mark_seen_upserts_read_state(db_session, user_factory) -> None:
    user_id, session_id = await _create_session(db_session, user_factory)
    service = ArtifactService(db_session)

    await service.mark_seen(user_id, session_id, "kol_analysis", "artifact-1")
    state = await db_session.scalar(
        select(ArtifactReadState).where(ArtifactReadState.user_id == user_id)
    )
    assert state is not None
    assert state.session_id == session_id
    assert state.module_key == "kol_analysis"
    assert state.last_seen_artifact_id == "artifact-1"

    await service.mark_seen(user_id, session_id, "kol_analysis", "artifact-2")
    states = list(
        (await db_session.scalars(select(ArtifactReadState))).all()
    )
    assert len(states) == 1
    assert states[0].last_seen_artifact_id == "artifact-2"
    # seen_at 已刷新（MySQL DATETIME 秒级精度，不做跨秒边界的严格比较）。
    assert states[0].seen_at is not None


@pytest.mark.asyncio
async def test_artifact_service_enforces_ownership(db_session, user_factory) -> None:
    _, session_id = await _create_session(db_session, user_factory)
    other = await user_factory()
    service = ArtifactService(db_session)

    with pytest.raises(LookupError, match="session_not_found"):
        await service.register_artifact(
            user_id=other.id,
            session_id=session_id,
            artifact_key="k",
            artifact_type="kol_report",
            title="t",
            version=1,
            report_id="r1",
        )
    with pytest.raises(LookupError, match="session_not_found"):
        await service.mark_seen(other.id, session_id, "kol_analysis", "artifact-1")
