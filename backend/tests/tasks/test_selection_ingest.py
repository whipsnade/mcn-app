from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.selection.service import KolSelectionService
from app.tasks.dependencies import DatabaseSelectionIngest


@pytest.mark.asyncio
async def test_ingest_retries_with_fresh_transaction_on_integrity_error(monkeypatch) -> None:
    calls: list[dict] = []

    async def fake_tool_mapping(self, db):
        return {"tool.x": "remote_x"}

    async def fake_ingest(self, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            # 并发 upsert 撞唯一约束：首次 commit 抛 IntegrityError。
            raise IntegrityError("INSERT", {}, Exception("duplicate entry"))

    monkeypatch.setattr(DatabaseSelectionIngest, "_tool_mapping", fake_tool_mapping)
    monkeypatch.setattr(KolSelectionService, "ingest_tool_evidence", fake_ingest)

    await DatabaseSelectionIngest().ingest(
        user_id="u",
        session_id="s",
        task_id="t",
        internal_tool_name="tool.x",
        structured_content={"result": "{}"},
    )

    # 新事务重试一次成功（第二次 select 会命中已有行走 merge）。
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_ingest_gives_up_after_retry_without_propagating(monkeypatch) -> None:
    calls = 0

    async def fake_tool_mapping(self, db):
        return {"tool.x": "remote_x"}

    async def fake_ingest(self, **kwargs):
        nonlocal calls
        calls += 1
        raise IntegrityError("INSERT", {}, Exception("duplicate entry"))

    monkeypatch.setattr(DatabaseSelectionIngest, "_tool_mapping", fake_tool_mapping)
    monkeypatch.setattr(KolSelectionService, "ingest_tool_evidence", fake_ingest)

    # 重试仍失败：只记 warning，不向任务循环抛错。
    await DatabaseSelectionIngest().ingest(
        user_id="u",
        session_id="s",
        task_id="t",
        internal_tool_name="tool.x",
        structured_content={"result": "{}"},
    )

    assert calls == 2


@pytest.mark.asyncio
async def test_ingest_skips_unapproved_tool_without_touching_db(monkeypatch) -> None:
    async def fake_tool_mapping(self, db):
        return {"tool.x": "remote_x"}

    called = False

    async def fake_ingest(self, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(DatabaseSelectionIngest, "_tool_mapping", fake_tool_mapping)
    monkeypatch.setattr(KolSelectionService, "ingest_tool_evidence", fake_ingest)

    await DatabaseSelectionIngest().ingest(
        user_id="u",
        session_id="s",
        task_id="t",
        internal_tool_name="tool.unknown",
        structured_content={},
    )

    assert called is False
