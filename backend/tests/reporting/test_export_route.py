from types import SimpleNamespace

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.reporting import router as reporting_router
from app.reporting.models import Kol, KolSnapshot, TaskCandidate
from app.tasks.models import AnalysisTask


def test_content_disposition_quotes_utf8_filename() -> None:
    value = reporting_router.content_disposition("科颜氏_美妆_KOL匹配度分析_20260716.xlsx")

    assert value.startswith("attachment; filename*=UTF-8''")
    assert "%E7%A7%91" in value


@pytest.mark.asyncio
async def test_latest_export_returns_stream_with_latest_pool(monkeypatch) -> None:
    task = SimpleNamespace(status="completed")
    pool = SimpleNamespace(id="pool-1")
    rows = [(object(), object(), object())]

    class FakeService:
        def __init__(self, db):
            self.db = db

        async def latest_candidate_pool(self, user_id, session_id):
            return task, pool, rows

    async def fake_export(db, user_id, session_id):
        return SimpleNamespace(
            content=b"xlsx",
            filename="品牌_KOL匹配度分析_20260716.xlsx",
        )

    monkeypatch.setattr(reporting_router, "ReportingService", FakeService)
    monkeypatch.setattr(reporting_router, "export_latest_task_xlsx", fake_export)

    response = await reporting_router.export_latest_session(
        "session-1", SimpleNamespace(id="user-1"), object()
    )

    assert response.media_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_latest_export_rejects_running_task(monkeypatch) -> None:
    class FakeService:
        def __init__(self, db):
            self.db = db

        async def latest_candidate_pool(self, user_id, session_id):
            return SimpleNamespace(status="running"), None, []

    monkeypatch.setattr(reporting_router, "ReportingService", FakeService)

    with pytest.raises(HTTPException, match="latest_task_in_progress") as error:
        await reporting_router.export_latest_session(
            "session-1", SimpleNamespace(id="user-1"), object()
        )
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_latest_export_accepts_legacy_candidate_rows_without_pool(monkeypatch) -> None:
    task = SimpleNamespace(status="completed")
    rows = [(object(), object(), object())]

    class FakeService:
        def __init__(self, db):
            self.db = db

        async def latest_candidate_pool(self, user_id, session_id):
            return task, None, rows

    async def fake_export(db, user_id, session_id):
        return SimpleNamespace(content=b"xlsx", filename="品牌_KOL匹配度分析.xlsx")

    monkeypatch.setattr(reporting_router, "ReportingService", FakeService)
    monkeypatch.setattr(reporting_router, "export_latest_task_xlsx", fake_export)

    response = await reporting_router.export_latest_session(
        "session-1", SimpleNamespace(id="user-1"), object()
    )

    assert response.media_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.mark.asyncio
async def test_latest_export_hides_deleted_session(auth_client_factory, db_session) -> None:
    client = await auth_client_factory("13300000041")
    created = await client.post(
        "/api/v1/sessions",
        json={
            "brand": "导出隔离品牌",
            "campaign_name": "软删除",
            "platforms": ["xiaohongshu"],
            "category": "美妆",
            "target_audience": "年轻女性",
            "initial_query": "创建导出任务",
        },
    )
    session_id = created.json()["id"]
    task_id = created.json()["latest_task"]["id"]
    now = datetime.now(UTC).replace(tzinfo=None)
    kol_id = str(uuid4())
    snapshot_id = str(uuid4())
    db_session.add_all(
        [
            Kol(
                id=kol_id,
                platform="xiaohongshu",
                platform_account_id=f"export-{kol_id}",
                normalized_profile_url=None,
                created_at=now,
                updated_at=now,
            ),
            KolSnapshot(
                id=snapshot_id,
                kol_id=kol_id,
                source_mcp_call_id=None,
                normalized_json={"nickname": "导出达人", "followers": 1000},
                collected_at=now,
                created_at=now,
            ),
            TaskCandidate(
                id=str(uuid4()),
                task_id=task_id,
                kol_id=kol_id,
                snapshot_id=snapshot_id,
                candidate_version=1,
                total_score=Decimal("80.000"),
                score_breakdown_json={"dimensions": {}},
                rank=1,
                matched_conditions_json=[],
                risk_flags_json=[],
                recommendation_text="推荐",
                evidence_json={},
                created_at=now,
            ),
        ]
    )
    task = await db_session.get(AnalysisTask, task_id)
    task.status = "completed"
    task.completed_at = now
    await db_session.flush()

    assert (await client.delete(f"/api/v1/sessions/{session_id}")).status_code == 204
    response = await client.get(f"/api/v1/sessions/{session_id}/exports/latest.xlsx")
    assert response.status_code == 404
