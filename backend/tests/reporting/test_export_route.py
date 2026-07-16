from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.reporting import router as reporting_router


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
