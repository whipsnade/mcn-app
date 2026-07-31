"""GET /sessions/{id}/reports?report_type=：按类型列出版本列表。"""

from __future__ import annotations

import pytest

from app.reporting.analysis_reports import AnalysisReportService
from app.reporting.blocks import MetricGridBlock, MetricItem, ReportDocument
from app.workspace.models import WorkspaceSession


def _document(title: str) -> ReportDocument:
    return ReportDocument(
        title=title,
        conclusion="结论。",
        blocks=[MetricGridBlock(items=[MetricItem(label="总声量", value=1200)])],
    )


async def _create_session(client) -> str:
    created = await client.post("/api/v1/sessions", json={})
    assert created.status_code == 201
    return created.json()["id"]


@pytest.mark.asyncio
async def test_reports_list_filters_by_type_and_orders_by_version_desc(
    auth_client_factory, db_session
) -> None:
    client = await auth_client_factory("13400000091")
    session_id = await _create_session(client)
    session = await db_session.get(WorkspaceSession, session_id)
    service = AnalysisReportService(db_session)
    await service.build_session_report(
        user_id=session.user_id,
        session_id=session_id,
        document=_document("品牌分析v1"),
        report_type="brand_analysis",
        scope={"brand": "海底捞"},
    )
    await service.build_session_report(
        user_id=session.user_id,
        session_id=session_id,
        document=_document("品牌分析v2"),
        report_type="brand_analysis",
        scope={"brand": "海底捞"},
    )
    await service.build_session_report(
        user_id=session.user_id,
        session_id=session_id,
        document=_document("活动复盘v1"),
        report_type="campaign_analysis",
        scope={"brand": "海底捞", "campaign": "618"},
    )
    await service.build_session_report(
        user_id=session.user_id, session_id=session_id, document=_document("KOL分析v1")
    )

    response = await client.get(
        f"/api/v1/sessions/{session_id}/reports", params={"report_type": "brand_analysis"}
    )

    assert response.status_code == 200
    items = response.json()
    assert [item["version"] for item in items] == [2, 1]
    first = items[0]
    assert first["title"] == "品牌分析v2"
    assert first["scope"] == {"brand": "海底捞"}
    assert first["status"] == "completed"
    assert first["report_id"]
    assert first["created_at"]

    campaign = await client.get(
        f"/api/v1/sessions/{session_id}/reports", params={"report_type": "campaign_analysis"}
    )
    assert [item["title"] for item in campaign.json()] == ["活动复盘v1"]
    kol = await client.get(
        f"/api/v1/sessions/{session_id}/reports", params={"report_type": "kol_analysis"}
    )
    assert [item["title"] for item in kol.json()] == ["KOL分析v1"]


@pytest.mark.asyncio
async def test_reports_list_empty_and_foreign(auth_client_factory) -> None:
    owner = await auth_client_factory("13400000092")
    other = await auth_client_factory("13400000093")
    session_id = await _create_session(owner)

    empty = await owner.get(
        f"/api/v1/sessions/{session_id}/reports", params={"report_type": "brand_analysis"}
    )
    assert empty.status_code == 200
    assert empty.json() == []

    foreign = await other.get(
        f"/api/v1/sessions/{session_id}/reports", params={"report_type": "brand_analysis"}
    )
    assert foreign.status_code == 404


@pytest.mark.asyncio
async def test_report_detail_includes_payload_but_list_does_not(
    auth_client_factory, db_session
) -> None:
    """详情端点带出 payload/template_version；版本列表项不含这两个字段。"""
    client = await auth_client_factory("13400000095")
    session_id = await _create_session(client)
    session = await db_session.get(WorkspaceSession, session_id)
    service = AnalysisReportService(db_session)
    report = await service.build_session_report(
        user_id=session.user_id,
        session_id=session_id,
        document=_document("品牌分析v1"),
        report_type="brand_analysis",
        scope={"brand": "海底捞"},
    )
    report.payload_json = {"summary": "结构化快照"}
    report.template_version = "brand_report_v2"
    legacy = await service.build_session_report(
        user_id=session.user_id,
        session_id=session_id,
        document=_document("品牌分析v2"),
        report_type="brand_analysis",
        scope={"brand": "海底捞"},
    )
    await db_session.flush()

    detail = await client.get(f"/api/v1/analysis-reports/{report.id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["payload"] == {"summary": "结构化快照"}
    assert body["template_version"] == "brand_report_v2"

    legacy_detail = await client.get(f"/api/v1/analysis-reports/{legacy.id}")
    assert legacy_detail.status_code == 200
    legacy_body = legacy_detail.json()
    assert legacy_body["payload"] is None
    assert legacy_body["template_version"] is None

    listing = await client.get(
        f"/api/v1/sessions/{session_id}/reports", params={"report_type": "brand_analysis"}
    )
    assert listing.status_code == 200
    assert len(listing.json()) == 2
    for item in listing.json():
        assert "payload" not in item
        assert "template_version" not in item


@pytest.mark.asyncio
async def test_reports_list_requires_valid_type(auth_client_factory) -> None:
    client = await auth_client_factory("13400000094")
    session_id = await _create_session(client)

    response = await client.get(
        f"/api/v1/sessions/{session_id}/reports", params={"report_type": "unknown"}
    )
    assert response.status_code == 422
    missing = await client.get(f"/api/v1/sessions/{session_id}/reports")
    assert missing.status_code == 422
