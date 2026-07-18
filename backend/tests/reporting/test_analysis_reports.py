import pytest
from sqlalchemy import select

from app.reporting.analysis_reports import AnalysisReportService
from app.reporting.blocks import HeadingBlock, MarkdownBlock, MetricGridBlock, MetricItem, ReportDocument
from app.tasks.models import TaskEvent


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
