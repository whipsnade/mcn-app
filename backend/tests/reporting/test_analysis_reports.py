import pytest
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, select

from app.reporting.analysis_reports import AnalysisReportService
from app.reporting.blocks import HeadingBlock, MarkdownBlock, MetricGridBlock, MetricItem, ReportDocument
from app.tasks.models import AnalysisTask, TaskEvent
from app.workspace.models import Message


def _document() -> ReportDocument:
    return ReportDocument(
        title="美妆行业分析",
        conclusion="行业保持增长。",
        blocks=[
            HeadingBlock(text="整体热度"),
            MetricGridBlock(
                items=[MetricItem(label="总声量", value=4373, unit="万帖", delta="+37.2%")]
            ),
            MarkdownBlock(text="同比大幅扩张但环比回落。"),
        ],
    )


async def _create_agent_session(client, suffix: str) -> tuple[str, str]:
    created = await client.post(
        "/api/v1/sessions",
        json={
            "brand": "",
            "campaign_name": None,
            "platforms": [],
            "category": "美妆",
            "target_audience": "",
            "initial_query": f"分析美妆行业在社交媒体的讨论热度和发展趋势 {suffix}",
        },
    )
    assert created.status_code == 201
    payload = created.json()
    assert payload["latest_task"]["kind"] == "agent"
    return payload["id"], payload["latest_task"]["id"]


@pytest.mark.asyncio
async def test_agent_task_report_build_is_idempotent_and_emits_event(
    auth_client_factory, db_session
) -> None:
    client = await auth_client_factory("13400000041")
    session_id, task_id = await _create_agent_session(client, "build")

    service = AnalysisReportService(db_session)
    report = await service.build(task_id, document=_document())

    assert report.version == 1
    assert report.session_id == session_id
    assert report.blocks_json[0]["type"] == "heading"

    again = await service.build(task_id, document=_document())
    assert again.id == report.id

    events = list(
        (
            await db_session.scalars(
                select(TaskEvent).where(
                    TaskEvent.task_id == task_id,
                    TaskEvent.event_type == "report.updated",
                )
            )
        ).all()
    )
    assert len(events) == 1
    assert events[0].payload_json["report_id"] == report.id


@pytest.mark.asyncio
async def test_analysis_report_read_and_session_summary(auth_client_factory, db_session) -> None:
    owner = await auth_client_factory("13400000043")
    session_id, task_id = await _create_agent_session(owner, "read")

    service = AnalysisReportService(db_session)
    report = await service.build(task_id, document=_document())

    response = await owner.get(f"/api/v1/analysis-reports/{report.id}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["title"] == "美妆行业分析"
    assert payload["blocks"][1]["type"] == "metric_grid"

    other = await auth_client_factory("13400000044")
    forbidden = await other.get(f"/api/v1/analysis-reports/{report.id}")
    assert forbidden.status_code == 404

    session_response = await owner.get(f"/api/v1/sessions/{session_id}")
    assert session_response.status_code == 200
    summary = session_response.json()["latest_analysis_report"]
    assert summary is not None
    assert summary["id"] == report.id
    assert summary["version"] == 1


@pytest.mark.asyncio
async def test_build_versions_increment_per_session(auth_client_factory, db_session) -> None:
    """迁移 0020 后 version 按会话编号：同一会话第二个任务的报告 version=2。"""
    client = await auth_client_factory("13400000045")
    session_id, task_id = await _create_agent_session(client, "version")

    service = AnalysisReportService(db_session)
    first = await service.build(task_id, document=_document())
    assert first.version == 1

    source = await db_session.get(AnalysisTask, task_id)
    assert source is not None
    now = datetime.now(UTC).replace(tzinfo=None)
    next_sequence = (
        await db_session.scalar(
            select(func.max(Message.sequence)).where(Message.session_id == session_id)
        )
        or 0
    ) + 1
    message = Message(
        id=str(uuid4()),
        session_id=session_id,
        user_id=source.user_id,
        role="user",
        content="继续分析",
        sequence=next_sequence,
        metadata_json={},
        created_at=now,
    )
    second_task = AnalysisTask(
        id=str(uuid4()),
        user_id=source.user_id,
        session_id=session_id,
        trigger_message_id=message.id,
        kind="agent",
        status="queued",
        max_calls=10,
        estimated_points=0,
        creation_order=2,
        created_at=now,
        updated_at=now,
    )
    db_session.add(message)
    await db_session.flush()
    db_session.add(second_task)
    await db_session.flush()

    second = await service.build(second_task.id, document=_document())
    assert second.version == 2
    assert second.session_id == session_id
    assert second.task_id == second_task.id


@pytest.mark.asyncio
async def test_build_session_report_increments_version(auth_client_factory, db_session) -> None:
    """会话级报告不幂等：每次点击生成新版本，task_id 为 NULL。"""
    client = await auth_client_factory("13400000046")
    session_id, task_id = await _create_agent_session(client, "session-report")
    source = await db_session.get(AnalysisTask, task_id)
    assert source is not None

    service = AnalysisReportService(db_session)
    first = await service.build_session_report(
        user_id=source.user_id, session_id=session_id, document=_document()
    )
    assert first.version == 1
    assert first.session_id == session_id
    assert first.task_id is None
    assert first.status == "completed"

    second = await service.build_session_report(
        user_id=source.user_id, session_id=session_id, document=_document()
    )
    assert second.version == 2
    assert second.id != first.id
    assert second.task_id is None

    latest = await service.latest_session_report(session_id)
    assert latest is not None
    assert latest.id == second.id
    assert latest.version == 2


@pytest.mark.asyncio
async def test_build_session_report_rejects_foreign_session(
    auth_client_factory, db_session
) -> None:
    client = await auth_client_factory("13400000047")
    session_id, _ = await _create_agent_session(client, "foreign-session")

    service = AnalysisReportService(db_session)
    with pytest.raises(LookupError, match="session_not_found"):
        await service.build_session_report(
            user_id=str(uuid4()), session_id=session_id, document=_document()
        )


@pytest.mark.asyncio
async def test_session_report_read_and_session_dto(auth_client_factory, db_session) -> None:
    """task_id 为 NULL 的会话级报告：owner 可读、他人 404、会话 DTO task_id 为 null。"""
    owner = await auth_client_factory("13400000048")
    session_id, task_id = await _create_agent_session(owner, "session-dto")
    source = await db_session.get(AnalysisTask, task_id)
    assert source is not None

    service = AnalysisReportService(db_session)
    report = await service.build_session_report(
        user_id=source.user_id, session_id=session_id, document=_document()
    )

    response = await owner.get(f"/api/v1/analysis-reports/{report.id}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == report.id
    assert payload["task_id"] is None
    assert payload["version"] == 1

    other = await auth_client_factory("13400000049")
    forbidden = await other.get(f"/api/v1/analysis-reports/{report.id}")
    assert forbidden.status_code == 404

    session_response = await owner.get(f"/api/v1/sessions/{session_id}")
    assert session_response.status_code == 200
    summary = session_response.json()["latest_analysis_report"]
    assert summary is not None
    assert summary["id"] == report.id
    assert summary["task_id"] is None

