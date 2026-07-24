from __future__ import annotations

from types import SimpleNamespace

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


@pytest.mark.asyncio
async def test_ingest_passes_arguments_through_to_service(monkeypatch) -> None:
    async def fake_tool_mapping(self, db):
        return {"tool.x": "remote_x"}

    captured: list[dict] = []

    async def fake_ingest(self, **kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(DatabaseSelectionIngest, "_tool_mapping", fake_tool_mapping)
    monkeypatch.setattr(KolSelectionService, "ingest_tool_evidence", fake_ingest)

    await DatabaseSelectionIngest().ingest(
        user_id="u",
        session_id="s",
        task_id="t",
        internal_tool_name="tool.x",
        structured_content={"result": "{}"},
        arguments={"platform": "xiaohongshu", "kwUidList": ["uid-1"]},
    )

    assert len(captured) == 1
    assert captured[0]["arguments"] == {"platform": "xiaohongshu", "kwUidList": ["uid-1"]}


@pytest.mark.asyncio
async def test_ingest_double_writes_to_set_and_legacy_when_goal_given(monkeypatch) -> None:
    async def fake_tool_mapping(self, db):
        return {"tool.x": "remote_x"}

    ensured: list[dict] = []
    set_ingested: list[dict] = []
    legacy_ingested: list[dict] = []

    async def fake_ensure(self, user_id, session_id, **kwargs):
        ensured.append({"user_id": user_id, "session_id": session_id, **kwargs})
        return SimpleNamespace(id="set-1")

    async def fake_set_ingest(self, **kwargs):
        set_ingested.append(kwargs)

    async def fake_legacy_ingest(self, **kwargs):
        legacy_ingested.append(kwargs)

    monkeypatch.setattr(DatabaseSelectionIngest, "_tool_mapping", fake_tool_mapping)
    monkeypatch.setattr(KolSelectionService, "ensure_selection_set", fake_ensure)
    monkeypatch.setattr(KolSelectionService, "ingest_tool_evidence_to_set", fake_set_ingest)
    monkeypatch.setattr(KolSelectionService, "ingest_tool_evidence", fake_legacy_ingest)

    await DatabaseSelectionIngest().ingest(
        user_id="u",
        session_id="s",
        task_id="t",
        internal_tool_name="tool.x",
        structured_content={"result": "{}"},
        goal_id="goal-1",
        set_title="默认名单",
        set_scope={"brand": "测试品牌"},
    )

    assert ensured == [
        {
            "user_id": "u",
            "session_id": "s",
            "task_id": "t",
            "goal_id": "goal-1",
            "title": "默认名单",
            "scope": {"brand": "测试品牌"},
        }
    ]
    assert len(set_ingested) == 1
    assert set_ingested[0]["selection_set_id"] == "set-1"
    assert set_ingested[0]["task_id"] == "t"
    assert len(legacy_ingested) == 1


@pytest.mark.asyncio
async def test_ingest_set_side_failure_does_not_block_legacy(monkeypatch, caplog) -> None:
    async def fake_tool_mapping(self, db):
        return {"tool.x": "remote_x"}

    legacy_ingested: list[dict] = []

    async def failing_ensure(self, user_id, session_id, **kwargs):
        raise RuntimeError("set side down")

    async def fake_legacy_ingest(self, **kwargs):
        legacy_ingested.append(kwargs)

    monkeypatch.setattr(DatabaseSelectionIngest, "_tool_mapping", fake_tool_mapping)
    monkeypatch.setattr(KolSelectionService, "ensure_selection_set", failing_ensure)
    monkeypatch.setattr(KolSelectionService, "ingest_tool_evidence", fake_legacy_ingest)

    with caplog.at_level("WARNING"):
        await DatabaseSelectionIngest().ingest(
            user_id="u",
            session_id="s",
            task_id="t",
            internal_tool_name="tool.x",
            structured_content={"result": "{}"},
            goal_id="goal-1",
        )

    # 新表失败只记 warning，旧表照常写入。
    assert len(legacy_ingested) == 1
    assert "kol_selection_set_ingest_failed" in caplog.text


@pytest.mark.asyncio
async def test_ingest_legacy_side_failure_does_not_block_set(monkeypatch) -> None:
    async def fake_tool_mapping(self, db):
        return {"tool.x": "remote_x"}

    set_ingested: list[dict] = []

    async def fake_ensure(self, user_id, session_id, **kwargs):
        return SimpleNamespace(id="set-1")

    async def fake_set_ingest(self, **kwargs):
        set_ingested.append(kwargs)

    async def failing_legacy_ingest(self, **kwargs):
        raise IntegrityError("INSERT", {}, Exception("duplicate entry"))

    monkeypatch.setattr(DatabaseSelectionIngest, "_tool_mapping", fake_tool_mapping)
    monkeypatch.setattr(KolSelectionService, "ensure_selection_set", fake_ensure)
    monkeypatch.setattr(KolSelectionService, "ingest_tool_evidence_to_set", fake_set_ingest)
    monkeypatch.setattr(KolSelectionService, "ingest_tool_evidence", failing_legacy_ingest)

    # 旧表两次重试均撞唯一约束：只记 warning，新表写入不受影响。
    await DatabaseSelectionIngest().ingest(
        user_id="u",
        session_id="s",
        task_id="t",
        internal_tool_name="tool.x",
        structured_content={"result": "{}"},
        goal_id="goal-1",
    )

    assert len(set_ingested) == 1


@pytest.mark.asyncio
async def test_ingest_without_goal_skips_set_side(monkeypatch) -> None:
    async def fake_tool_mapping(self, db):
        return {"tool.x": "remote_x"}

    legacy_ingested: list[dict] = []

    async def forbidden_ensure(self, user_id, session_id, **kwargs):
        raise AssertionError("ensure_selection_set should not be called without goal")

    async def fake_legacy_ingest(self, **kwargs):
        legacy_ingested.append(kwargs)

    monkeypatch.setattr(DatabaseSelectionIngest, "_tool_mapping", fake_tool_mapping)
    monkeypatch.setattr(KolSelectionService, "ensure_selection_set", forbidden_ensure)
    monkeypatch.setattr(KolSelectionService, "ingest_tool_evidence", fake_legacy_ingest)

    await DatabaseSelectionIngest().ingest(
        user_id="u",
        session_id="s",
        task_id="t",
        internal_tool_name="tool.x",
        structured_content={"result": "{}"},
    )

    assert len(legacy_ingested) == 1
