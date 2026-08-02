"""历史读取工具测试（设计文档 §九 / §10.2）。

read_artifact / search_evidence / read_tool_result：每个工具校验
Evidence/Artifact 属于当前用户和 Session；跨用户/跨 Session 返回结构化
not_found/forbidden（Router 层统一映射为 404）。大结果只返回有限分片 +
next_cursor + truncated。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select

from app.agent_artifacts.models import (
    AgentArtifact,
    AgentArtifactVersion,
    ArtifactDraft,
    ArtifactDraftRevision,
)
from app.agent_runtime.evidence import EvidenceWriter
from app.agent_runtime.models import (
    AgentRun,
    AgentRunAttempt,
    AgentSession,
    AgentStep,
    AgentToolCall,
)
from app.agent_runtime.tools.contracts import ToolContext
from app.agent_runtime.tools.history import (
    FORBIDDEN,
    INVALID_ARGUMENTS,
    NOT_FOUND,
    ReadArtifactTool,
    ReadToolResultTool,
    SearchEvidenceTool,
)

CTX = ToolContext(
    user_id="u-1",
    session_id="s-1",
    run_id="r-1",
    profile_name="session_analyst_v1",
)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _summary(result) -> dict:
    assert result.status == "success", result.safe_summary
    return json.loads(result.safe_summary)


async def _make_chain(
    db_session, user_id: str, *, title: str = "会话"
) -> tuple[AgentSession, AgentRun, AgentStep, AgentToolCall]:
    now = _now()
    session = AgentSession(
        id=str(uuid4()), user_id=user_id, title=title, status="active", created_at=now, updated_at=now
    )
    db_session.add(session)
    await db_session.flush()
    run = AgentRun(
        id=str(uuid4()),
        session_id=session.id,
        user_id=user_id,
        profile_name="session_analyst_v1",
        profile_version="v1",
        model="test-model",
        status="running",
    )
    db_session.add(run)
    await db_session.flush()
    attempt = AgentRunAttempt(id=str(uuid4()), run_id=run.id, attempt=1, started_at=now)
    db_session.add(attempt)
    await db_session.flush()
    step = AgentStep(
        id=str(uuid4()),
        run_id=run.id,
        attempt_id=attempt.id,
        sequence=1,
        step_type="tool_call",
        status="running",
        created_at=now,
    )
    db_session.add(step)
    await db_session.flush()
    call = AgentToolCall(
        id=str(uuid4()),
        run_id=run.id,
        step_id=step.id,
        logical_call_id=str(uuid4()),
        service="internal",
        internal_tool_name="seed",
        arguments_json={},
        arguments_hash="h" * 64,
        status="settled",
    )
    db_session.add(call)
    await db_session.flush()
    return session, run, step, call


async def _make_artifact(
    db_session,
    user_id: str,
    session: AgentSession,
    run: AgentRun,
    *,
    payload: dict,
    artifact_type: str = "report_v1",
    data_status: str = "complete",
    artifact_key: str = "brand/report",
    version: int = 1,
    artifact_id: str | None = None,
) -> AgentArtifact:
    now = _now()
    artifact = AgentArtifact(
        id=artifact_id or str(uuid4()),
        session_id=session.id,
        user_id=user_id,
        module="brand",
        artifact_type=artifact_type,
        parent_artifact_id=None,
        artifact_key=artifact_key,
        status="published",
        latest_version=version,
        activity_sequence=0,
        created_at=now,
        updated_at=now,
    )
    db_session.add(artifact)
    await db_session.flush()
    draft = ArtifactDraft(
        id=str(uuid4()),
        artifact_id=artifact.id,
        session_id=session.id,
        owner_run_id=run.id,
        current_revision=0,
        status="idle",
        updated_at=now,
    )
    db_session.add(draft)
    await db_session.flush()
    draft_rev = ArtifactDraftRevision(
        id=str(uuid4()),
        draft_id=draft.id,
        artifact_id=artifact.id,
        run_id=run.id,
        revision=0,
        schema_version="v1",
        payload_json=payload,
        payload_hash="h" * 64,
        created_at=now,
    )
    db_session.add(draft_rev)
    await db_session.flush()
    artifact_version = AgentArtifactVersion(
        id=str(uuid4()),
        artifact_id=artifact.id,
        version=version,
        source_run_id=run.id,
        source_draft_revision_id=draft_rev.id,
        schema_version="v1",
        payload_json=payload,
        data_status=data_status,
        created_at=now,
    )
    db_session.add(artifact_version)
    await db_session.flush()
    return artifact


async def _make_evidence(
    db_session,
    session: AgentSession,
    run: AgentRun,
    call: AgentToolCall,
    *,
    raw_payload: object,
    source_name: str = "query_analysis_data",
) -> str:
    writer = EvidenceWriter(db_session)
    item = await writer.write(
        session_id=session.id,
        run_id=run.id,
        tool_call_id=call.id,
        source_type="mcp",
        source_name=source_name,
        scope_json={"brand": "美妆"},
        period_json=None,
        raw_payload=raw_payload,
        collected_at=_now(),
        availability_status="available",
    )
    return item.id


# ---------------------------------------------------------------------------
# read_artifact
# ---------------------------------------------------------------------------


async def test_read_artifact_returns_published_payload(db_session, user_factory) -> None:
    user = await user_factory()
    session, run, _step, _call = await _make_chain(db_session, user.id)
    payload = {"title": "品牌报告", "data": {"overview": {"total_volume": 100}}}
    artifact = await _make_artifact(db_session, user.id, session, run, payload=payload)
    context = ToolContext(user_id=user.id, session_id=session.id, run_id=run.id, profile_name="session_analyst_v1")

    tool = ReadArtifactTool(db_session)
    result = await tool.execute(context, type(tool).input_model(artifact_id=artifact.id))
    data = _summary(result)
    assert data["artifact_id"] == artifact.id
    assert data["payload"] == payload


async def test_read_artifact_section(db_session, user_factory) -> None:
    user = await user_factory()
    session, run, _step, _call = await _make_chain(db_session, user.id)
    payload = {"title": "品牌报告", "data": {"overview": {"total_volume": 100}}}
    artifact = await _make_artifact(db_session, user.id, session, run, payload=payload)
    context = ToolContext(user_id=user.id, session_id=session.id, run_id=run.id, profile_name="session_analyst_v1")

    tool = ReadArtifactTool(db_session)
    result = await tool.execute(
        context, type(tool).input_model(artifact_id=artifact.id, section="data.overview")
    )
    data = _summary(result)
    assert data["section"] == "data.overview"
    assert data["payload"] == {"total_volume": 100}


async def test_read_artifact_specific_version(db_session, user_factory) -> None:
    user = await user_factory()
    session, run, _step, _call = await _make_chain(db_session, user.id)
    payload_v1 = {"title": "v1"}
    artifact = await _make_artifact(db_session, user.id, session, run, payload=payload_v1, version=1)
    payload_v2 = {"title": "v2"}
    # 为同一 artifact 追加版本 2：复用既有 draft（draft 与 artifact 一对一），
    # 追加一条 draft revision 0 -> 发布版本 2。
    now = _now()
    draft = await db_session.scalar(
        select(ArtifactDraft).where(ArtifactDraft.artifact_id == artifact.id)
    )
    assert draft is not None
    draft_rev = ArtifactDraftRevision(
        id=str(uuid4()),
        draft_id=draft.id,
        artifact_id=artifact.id,
        run_id=run.id,
        revision=1,
        schema_version="v1",
        payload_json=payload_v2,
        payload_hash="h" * 64,
        created_at=now,
    )
    db_session.add(draft_rev)
    await db_session.flush()
    db_session.add(
        AgentArtifactVersion(
            id=str(uuid4()),
            artifact_id=artifact.id,
            version=2,
            source_run_id=run.id,
            source_draft_revision_id=draft_rev.id,
            schema_version="v1",
            payload_json=payload_v2,
            data_status="complete",
            created_at=now,
        )
    )
    await db_session.flush()
    artifact.latest_version = 2
    await db_session.flush()

    context = ToolContext(user_id=user.id, session_id=session.id, run_id=run.id, profile_name="session_analyst_v1")
    tool = ReadArtifactTool(db_session)
    result = await tool.execute(
        context, type(tool).input_model(artifact_id=artifact.id, version=1)
    )
    assert _summary(result)["payload"] == payload_v1
    latest = await tool.execute(
        context, type(tool).input_model(artifact_id=artifact.id)
    )
    assert _summary(latest)["payload"] == payload_v2


async def test_read_artifact_missing_version_not_found(db_session, user_factory) -> None:
    user = await user_factory()
    session, run, _step, _call = await _make_chain(db_session, user.id)
    artifact = await _make_artifact(db_session, user.id, session, run, payload={"title": "v1"}, version=1)
    context = ToolContext(user_id=user.id, session_id=session.id, run_id=run.id, profile_name="session_analyst_v1")

    tool = ReadArtifactTool(db_session)
    result = await tool.execute(
        context, type(tool).input_model(artifact_id=artifact.id, version=99)
    )
    assert result.status == "failed"
    assert result.error_type == NOT_FOUND


async def test_read_artifact_missing_not_found(db_session, user_factory) -> None:
    user = await user_factory()
    session, _run, _step, _call = await _make_chain(db_session, user.id)
    context = ToolContext(user_id=user.id, session_id=session.id, run_id="r-x", profile_name="session_analyst_v1")

    tool = ReadArtifactTool(db_session)
    result = await tool.execute(
        context, type(tool).input_model(artifact_id="does-not-exist")
    )
    assert result.status == "failed"
    assert result.error_type == NOT_FOUND


async def test_read_artifact_cross_user_forbidden(db_session, user_factory) -> None:
    owner = await user_factory()
    other = await user_factory()
    session, run, _step, _call = await _make_chain(db_session, owner.id)
    artifact = await _make_artifact(db_session, owner.id, session, run, payload={"title": "品牌报告"})
    context = ToolContext(user_id=other.id, session_id=session.id, run_id=run.id, profile_name="session_analyst_v1")

    tool = ReadArtifactTool(db_session)
    result = await tool.execute(context, type(tool).input_model(artifact_id=artifact.id))
    assert result.status == "failed"
    assert result.error_type == FORBIDDEN


# ---------------------------------------------------------------------------
# search_evidence
# ---------------------------------------------------------------------------


async def test_search_evidence_scoped_to_session(db_session, user_factory) -> None:
    user = await user_factory()
    session1, run1, _step, call1 = await _make_chain(db_session, user.id, title="会话1")
    session2, run2, _step, call2 = await _make_chain(db_session, user.id, title="会话2")
    ev1 = await _make_evidence(db_session, session1, run1, call1, raw_payload={"result": "{\"rows\": [{\"关键词\": \"美妆\"}]}"})
    await _make_evidence(db_session, session2, run2, call2, raw_payload={"result": "{\"rows\": [{\"关键词\": \"美食\"}]}"})
    context = ToolContext(user_id=user.id, session_id=session1.id, run_id=run1.id, profile_name="session_analyst_v1")

    tool = SearchEvidenceTool(db_session)
    result = await tool.execute(context, type(tool).input_model(query="美妆"))
    data = _summary(result)
    ids = {m["evidence_id"] for m in data["matches"]}
    assert ev1 in ids
    # 跨 Session 的 evidence 不会被返回。
    assert len(data["matches"]) == 1


async def test_search_evidence_run_scoped(db_session, user_factory) -> None:
    user = await user_factory()
    session, run1, _step, call1 = await _make_chain(db_session, user.id)
    run2 = AgentRun(
        id=str(uuid4()),
        session_id=session.id,
        user_id=user.id,
        profile_name="session_analyst_v1",
        profile_version="v1",
        model="test-model",
        status="running",
    )
    db_session.add(run2)
    await db_session.flush()
    ev1 = await _make_evidence(db_session, session, run1, call1, raw_payload={"result": "{\"rows\": [{\"关键词\": \"美妆\"}]}"})
    context = ToolContext(user_id=user.id, session_id=session.id, run_id=run1.id, profile_name="session_analyst_v1")

    tool = SearchEvidenceTool(db_session)
    result = await tool.execute(context, type(tool).input_model(query="", run_id=run1.id))
    data = _summary(result)
    assert {m["evidence_id"] for m in data["matches"]} == {ev1}


async def test_search_evidence_cross_session_not_returned(db_session, user_factory) -> None:
    user = await user_factory()
    session, run, _step, call = await _make_chain(db_session, user.id)
    await _make_evidence(db_session, session, run, call, raw_payload={"result": "{\"rows\": [{\"关键词\": \"美妆\"}]}"})
    # 另一用户完全不同的会话。
    other = await user_factory()
    other_session, other_run, _step, other_call = await _make_chain(db_session, other.id, title="他人会话")
    await _make_evidence(db_session, other_session, other_run, other_call, raw_payload={"result": "{\"rows\": [{\"关键词\": \"美妆\"}]}"})

    context = ToolContext(user_id=user.id, session_id=session.id, run_id=run.id, profile_name="session_analyst_v1")
    tool = SearchEvidenceTool(db_session)
    result = await tool.execute(context, type(tool).input_model(query="美妆"))
    assert len(_summary(result)["matches"]) == 1


async def test_search_evidence_cross_user_forbidden(db_session, user_factory) -> None:
    owner = await user_factory()
    other = await user_factory()
    session, run, _step, _call = await _make_chain(db_session, owner.id)
    context = ToolContext(user_id=other.id, session_id=session.id, run_id=run.id, profile_name="session_analyst_v1")

    tool = SearchEvidenceTool(db_session)
    result = await tool.execute(context, type(tool).input_model(query="美妆"))
    assert result.status == "failed"
    assert result.error_type == FORBIDDEN


# ---------------------------------------------------------------------------
# read_tool_result
# ---------------------------------------------------------------------------


async def test_read_tool_result_chunked_pages(db_session, user_factory) -> None:
    user = await user_factory()
    session, run, _step, call = await _make_chain(db_session, user.id)
    payload = {"rows": [{"i": i, "text": "x" * 10} for i in range(10)]}
    evidence_id = await _make_evidence(db_session, session, run, call, raw_payload=payload)
    context = ToolContext(user_id=user.id, session_id=session.id, run_id=run.id, profile_name="session_analyst_v1")

    tool = ReadToolResultTool(db_session)
    first = _summary(await tool.execute(context, type(tool).input_model(evidence_id=evidence_id, limit=4)))
    assert first["evidence_id"] == evidence_id
    assert len(first["items"]) == 4
    assert first["total"] == 10
    assert first["next_cursor"] == "4"
    assert first["truncated"] is True

    second = _summary(await tool.execute(context, type(tool).input_model(evidence_id=evidence_id, cursor=4, limit=4)))
    assert [item["i"] for item in second["items"]] == [4, 5, 6, 7]
    assert second["next_cursor"] == "8"

    third = _summary(await tool.execute(context, type(tool).input_model(evidence_id=evidence_id, cursor=8, limit=4)))
    assert [item["i"] for item in third["items"]] == [8, 9]
    assert third["next_cursor"] is None
    assert third["truncated"] is False


async def test_read_tool_result_missing_not_found(db_session, user_factory) -> None:
    user = await user_factory()
    session, _run, _step, _call = await _make_chain(db_session, user.id)
    context = ToolContext(user_id=user.id, session_id=session.id, run_id="r-x", profile_name="session_analyst_v1")

    tool = ReadToolResultTool(db_session)
    result = await tool.execute(context, type(tool).input_model(evidence_id="nope"))
    assert result.status == "failed"
    assert result.error_type == NOT_FOUND


async def test_read_tool_result_cross_user_forbidden(db_session, user_factory) -> None:
    owner = await user_factory()
    other = await user_factory()
    session, run, _step, call = await _make_chain(db_session, owner.id)
    evidence_id = await _make_evidence(db_session, session, run, call, raw_payload={"rows": []})
    context = ToolContext(user_id=other.id, session_id=session.id, run_id=run.id, profile_name="session_analyst_v1")

    tool = ReadToolResultTool(db_session)
    result = await tool.execute(context, type(tool).input_model(evidence_id=evidence_id))
    assert result.status == "failed"
    assert result.error_type == FORBIDDEN


async def test_read_tool_result_oversized_limit_rejected(db_session, user_factory) -> None:
    user = await user_factory()
    session, run, _step, call = await _make_chain(db_session, user.id)
    evidence_id = await _make_evidence(
        db_session, session, run, call, raw_payload={"rows": [{"i": i} for i in range(300)]}
    )
    context = ToolContext(user_id=user.id, session_id=session.id, run_id=run.id, profile_name="session_analyst_v1")
    tool = ReadToolResultTool(db_session)
    # 以 dict 传入，触发 execute 内参数校验（构造时 le=200 会直接拦截）。
    result = await tool.execute(context, {"evidence_id": evidence_id, "limit": 10**9})
    assert result.status == "failed"
    assert result.error_type == INVALID_ARGUMENTS
    # 合法但大的 limit 也不超上限：永远不整页返回大结果（§10.2）。
    bounded = await tool.execute(
        context, type(tool).input_model(evidence_id=evidence_id, limit=200)
    )
    assert bounded.status == "success"
    assert len(json.loads(bounded.safe_summary)["items"]) == 200


async def test_search_evidence_does_not_load_raw_payload_columns(db_session, user_factory) -> None:
    from sqlalchemy import event

    from app.db.session import engine

    captured: list[str] = []

    @event.listens_for(engine.sync_engine, "before_execute")
    def _capture(conn, clause, multiparams, params, execution_options):
        captured.append(str(clause))

    try:
        user = await user_factory()
        session, run, _step, call = await _make_chain(db_session, user.id)
        await _make_evidence(db_session, session, run, call, raw_payload={"rows": [{"关键词": "美妆"}]})
        context = ToolContext(user_id=user.id, session_id=session.id, run_id=run.id, profile_name="session_analyst_v1")
        tool = SearchEvidenceTool(db_session)
        result = await tool.execute(context, type(tool).input_model(query="美妆"))
        assert result.status == "success"
        assert json.loads(result.safe_summary)["total_matches"] == 1
    finally:
        event.remove(engine.sync_engine, "before_execute", _capture)

    # search_evidence 只投影匹配/展示列，绝不加载大字段 raw_payload_json。
    search_selects = [
        sql
        for sql in captured
        if sql.lstrip().lower().startswith("select") and "evidence_items" in sql
    ]
    assert search_selects
    for sql in search_selects:
        assert "raw_payload_json" not in sql
