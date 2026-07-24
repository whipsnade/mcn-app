"""artifacts summary 与已读状态端点。"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.artifacts.models import ArtifactReadState, TaskArtifact
from app.artifacts.service import ArtifactService
from app.reporting.analysis_reports import AnalysisReportService
from app.reporting.blocks import MetricGridBlock, MetricItem, ReportDocument
from app.selection.service import KolSelectionService
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


async def _register(
    db_session,
    user_id: str,
    session_id: str,
    *,
    key: str,
    artifact_type: str,
    title: str,
    version: int,
    status: str = "completed",
    report_id: str | None = None,
    selection_set_id: str | None = None,
    scope: dict | None = None,
) -> TaskArtifact:
    return await ArtifactService(db_session).register_artifact(
        user_id=user_id,
        session_id=session_id,
        artifact_key=key,
        artifact_type=artifact_type,
        title=title,
        version=version,
        status=status,
        report_id=report_id,
        selection_set_id=selection_set_id,
        scope=scope,
    )


@pytest.mark.asyncio
async def test_artifacts_summary_empty_session(auth_client_factory) -> None:
    client = await auth_client_factory("13400000095")
    session_id = await _create_session(client)

    response = await client.get(f"/api/v1/sessions/{session_id}/artifacts/summary")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"brand", "campaign", "kol_analysis", "kol_selection"}
    for module in body.values():
        assert module == {"latest_artifact": None, "unread": False}


@pytest.mark.asyncio
async def test_artifacts_summary_unread_states(auth_client_factory, db_session) -> None:
    client = await auth_client_factory("13400000096")
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
    brand_v2 = await service.build_session_report(
        user_id=session.user_id,
        session_id=session_id,
        document=_document("品牌分析v2"),
        report_type="brand_analysis",
        scope={"brand": "海底捞"},
    )
    brand_artifact = await _register(
        db_session,
        session.user_id,
        session_id,
        key="goal:g1:brand_report",
        artifact_type="brand_report",
        title="品牌分析v2",
        version=2,
        report_id=brand_v2.id,
        scope={"brand": "海底捞"},
    )
    kol_report = await service.build_session_report(
        user_id=session.user_id, session_id=session_id, document=_document("KOL分析v1")
    )
    kol_artifact = await _register(
        db_session,
        session.user_id,
        session_id,
        key="goal:g2:kol_report",
        artifact_type="kol_report",
        title="KOL分析v1",
        version=1,
        report_id=kol_report.id,
    )
    selection_set = await KolSelectionService(db_session).ensure_selection_set(
        session.user_id, session_id, title="默认名单"
    )
    await _register(
        db_session,
        session.user_id,
        session_id,
        key="goal:g3:kol_selection_set",
        artifact_type="kol_selection_set",
        title="默认名单",
        version=1,
        selection_set_id=selection_set.id,
    )
    # kol_analysis 模块已读最新；kol_selection 已读旧版（不存在旧版 artifact，手动构造）。
    service_artifacts = ArtifactService(db_session)
    await service_artifacts.mark_seen(session.user_id, session_id, "kol_analysis", kol_artifact.id)
    await service_artifacts.mark_seen(session.user_id, session_id, "kol_selection", "stale-artifact-id")

    response = await client.get(f"/api/v1/sessions/{session_id}/artifacts/summary")

    assert response.status_code == 200
    body = response.json()
    # brand：无已读记录 → unread。
    assert body["brand"]["unread"] is True
    assert body["brand"]["latest_artifact"]["artifact_id"] == brand_artifact.id
    assert body["brand"]["latest_artifact"]["artifact_type"] == "brand_report"
    assert body["brand"]["latest_artifact"]["version"] == 2
    assert body["brand"]["latest_artifact"]["scope"] == {"brand": "海底捞"}
    # kol_analysis：已读最新 → 不 unread。
    assert body["kol_analysis"]["unread"] is False
    assert body["kol_analysis"]["latest_artifact"]["artifact_id"] == kol_artifact.id
    # kol_selection：已读 id 不是最新 → unread。
    assert body["kol_selection"]["unread"] is True
    # campaign：无产物 → null 且不 unread。
    assert body["campaign"] == {"latest_artifact": None, "unread": False}


@pytest.mark.asyncio
async def test_artifacts_summary_foreign_session_404(auth_client_factory) -> None:
    owner = await auth_client_factory("13400000097")
    other = await auth_client_factory("13400000098")
    session_id = await _create_session(owner)

    response = await other.get(f"/api/v1/sessions/{session_id}/artifacts/summary")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_mark_seen_endpoint_writes_read_state(auth_client_factory, db_session) -> None:
    client = await auth_client_factory("13400000099")
    session_id = await _create_session(client)
    session = await db_session.get(WorkspaceSession, session_id)
    report = await AnalysisReportService(db_session).build_session_report(
        user_id=session.user_id,
        session_id=session_id,
        document=_document("品牌分析v1"),
        report_type="brand_analysis",
    )
    artifact = await _register(
        db_session,
        session.user_id,
        session_id,
        key="goal:g1:brand_report",
        artifact_type="brand_report",
        title="品牌分析v1",
        version=1,
        report_id=report.id,
    )

    response = await client.put(
        f"/api/v1/sessions/{session_id}/artifact-read-state",
        json={"module_key": "brand", "artifact_id": artifact.id},
    )

    assert response.status_code == 204
    state = await db_session.scalar(
        select(ArtifactReadState).where(
            ArtifactReadState.session_id == session_id,
            ArtifactReadState.module_key == "brand",
        )
    )
    assert state is not None
    assert state.last_seen_artifact_id == artifact.id
    # summary 不再 unread。
    summary = await client.get(f"/api/v1/sessions/{session_id}/artifacts/summary")
    assert summary.json()["brand"]["unread"] is False


@pytest.mark.asyncio
async def test_mark_seen_rejects_foreign_artifact(auth_client_factory, db_session) -> None:
    owner = await auth_client_factory("13400000100")
    other = await auth_client_factory("13400000101")
    session_id = await _create_session(owner)
    other_session_id = await _create_session(other)
    session = await db_session.get(WorkspaceSession, session_id)
    other_session = await db_session.get(WorkspaceSession, other_session_id)
    report = await AnalysisReportService(db_session).build_session_report(
        user_id=other_session.user_id,
        session_id=other_session_id,
        document=_document("他人报告"),
        report_type="brand_analysis",
    )
    foreign_artifact = await _register(
        db_session,
        other_session.user_id,
        other_session_id,
        key="goal:other:brand_report",
        artifact_type="brand_report",
        title="他人报告",
        version=1,
        report_id=report.id,
    )

    # artifact 不属于该会话 → 404；跨用户会话 → 404。
    mismatch = await owner.put(
        f"/api/v1/sessions/{session_id}/artifact-read-state",
        json={"module_key": "brand", "artifact_id": foreign_artifact.id},
    )
    assert mismatch.status_code == 404
    foreign = await other.put(
        f"/api/v1/sessions/{session_id}/artifact-read-state",
        json={"module_key": "brand", "artifact_id": foreign_artifact.id},
    )
    assert foreign.status_code == 404
    invalid_module = await owner.put(
        f"/api/v1/sessions/{session_id}/artifact-read-state",
        json={"module_key": "unknown", "artifact_id": foreign_artifact.id},
    )
    assert invalid_module.status_code == 422
    _ = session
