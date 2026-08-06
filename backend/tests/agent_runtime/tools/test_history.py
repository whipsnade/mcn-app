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
    AgentMessage,
    AgentRun,
    AgentRunAttempt,
    AgentSession,
    AgentStep,
    AgentToolCall,
    MemoryEntry,
)
from app.agent_runtime.repository import utc_now
from app.agent_runtime.tools.contracts import ToolContext
from app.agent_runtime.tools.history import (
    FORBIDDEN,
    INVALID_ARGUMENTS,
    NOT_FOUND,
    ReadArtifactTool,
    ReadToolResultTool,
    RememberScopeArgs,
    RememberScopeTool,
    SearchEvidenceTool,
    _MAX_TOOL_RESULT_TOTAL_CHARS,
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


async def test_read_artifact_cross_session_published_allowed(db_session, user_factory) -> None:
    """同用户跨 Session 可读已发布 Version（§5.4 历史复用）；活动 Draft 仍限
    本 Session，跨 Session 时走已发布路径。"""
    user = await user_factory()
    session_a, run_a, _step_a, _call_a = await _make_chain(db_session, user.id)
    session_b, run_b, _step_b, _call_b = await _make_chain(db_session, user.id)
    artifact = await _make_artifact(
        db_session, user.id, session_a, run_a, payload={"title": "跨会话报告"}
    )
    # 当前 Run 在 session_b，读 session_a 的已发布 Artifact。
    context = ToolContext(
        user_id=user.id, session_id=session_b.id, run_id=run_b.id, profile_name="session_analyst_v1"
    )

    tool = ReadArtifactTool(db_session)
    result = await tool.execute(context, type(tool).input_model(artifact_id=artifact.id))
    assert result.status == "success"
    data = json.loads(result.safe_summary)
    assert data["status"] == "published"
    assert data["payload"]["title"] == "跨会话报告"


# ---------------------------------------------------------------------------
# read_artifact 读取未发布 Draft（F1）：Builder 产出 Draft 后模型需要验证内容；
# 活动 Draft（drafting/reviewing）优先于已发布 Version，发布语义不变。
# ---------------------------------------------------------------------------


async def _make_draft_only_artifact(
    db_session,
    user_id: str,
    session: AgentSession,
    run: AgentRun,
    *,
    payload: dict,
    draft_status: str = "drafting",
    artifact_id: str | None = None,
) -> tuple[AgentArtifact, ArtifactDraft, ArtifactDraftRevision]:
    """落一个只有活动 Draft（无已发布 Version）的 Artifact。"""
    now = _now()
    artifact = AgentArtifact(
        id=artifact_id or str(uuid4()),
        session_id=session.id,
        user_id=user_id,
        module="brand",
        artifact_type="brand_report_v3",
        parent_artifact_id=None,
        artifact_key="brand/draft-only",
        status="draft",
        latest_version=0,
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
        current_revision=1,
        status=draft_status,
        updated_at=now,
    )
    db_session.add(draft)
    await db_session.flush()
    revision = ArtifactDraftRevision(
        id=str(uuid4()),
        draft_id=draft.id,
        artifact_id=artifact.id,
        run_id=run.id,
        revision=1,
        schema_version="brand_report_v3",
        payload_json=payload,
        payload_hash="h" * 64,
        created_at=now,
    )
    db_session.add(revision)
    await db_session.flush()
    return artifact, draft, revision


async def test_read_artifact_reads_active_draft(db_session, user_factory) -> None:
    """Builder 刚落 Draft（尚无已发布 Version）→ read_artifact 读 Draft 并标注。"""
    user = await user_factory()
    session, run, _step, _call = await _make_chain(db_session, user.id)
    payload = {"title": "品牌报告草稿", "data": {"overview": {"total_volume": 100}}}
    artifact, draft, _revision = await _make_draft_only_artifact(
        db_session, user.id, session, run, payload=payload
    )
    context = ToolContext(user_id=user.id, session_id=session.id, run_id=run.id, profile_name="session_analyst_v1")

    tool = ReadArtifactTool(db_session)
    result = await tool.execute(context, type(tool).input_model(artifact_id=artifact.id))
    data = _summary(result)
    assert data["status"] == "draft"
    assert data["revision"] == 1
    assert data["draft_id"] == draft.id
    assert data["payload"] == payload


async def test_read_artifact_draft_section_rfc6901_slice(db_session, user_factory) -> None:
    """Draft 的 section 参数按 RFC6901 切片（supporting_paths 同形路径）。"""
    user = await user_factory()
    session, run, _step, _call = await _make_chain(db_session, user.id)
    payload = {"title": "草稿", "data": {"overview": {"total_volume": 100}}}
    artifact, _draft, _revision = await _make_draft_only_artifact(
        db_session, user.id, session, run, payload=payload
    )
    context = ToolContext(user_id=user.id, session_id=session.id, run_id=run.id, profile_name="session_analyst_v1")

    tool = ReadArtifactTool(db_session)
    result = await tool.execute(
        context,
        type(tool).input_model(artifact_id=artifact.id, section="/data/overview"),
    )
    data = _summary(result)
    assert data["status"] == "draft"
    assert data["section"] == "/data/overview"
    assert data["payload"] == {"total_volume": 100}

    missing = await tool.execute(
        context,
        type(tool).input_model(artifact_id=artifact.id, section="/data/nonexistent"),
    )
    assert missing.status == "failed"
    assert missing.error_type == NOT_FOUND


async def test_read_artifact_draft_cross_session_not_found(db_session, user_factory) -> None:
    """同用户其他 Session 的活动 Draft 不泄漏存在性：跨 Session 只读已发布
    Version，活动 Draft 限本 Session，读不到即 not_found（与已发布路径一致）。"""
    user = await user_factory()
    session, run, _step, _call = await _make_chain(db_session, user.id)
    artifact, _draft, _revision = await _make_draft_only_artifact(
        db_session, user.id, session, run, payload={"title": "私有草稿"}
    )
    other_session, other_run, _s2, _c2 = await _make_chain(db_session, user.id)
    context = ToolContext(
        user_id=user.id,
        session_id=other_session.id,
        run_id=other_run.id,
        profile_name="session_analyst_v1",
    )

    tool = ReadArtifactTool(db_session)
    result = await tool.execute(context, type(tool).input_model(artifact_id=artifact.id))
    assert result.status == "failed"
    assert result.error_type == NOT_FOUND


async def test_read_artifact_draft_preferred_then_published_after_release(
    db_session, user_factory,
) -> None:
    """发布前后状态标注：活动中（reviewing）读 Draft；Draft 释放回 idle 后
    回到已发布 Version（status=published），显式 version 恒读已发布。"""
    user = await user_factory()
    session, run, _step, _call = await _make_chain(db_session, user.id)
    published_payload = {"title": "已发布", "data": {"overview": {"total_volume": 1}}}
    artifact = await _make_artifact(
        db_session, user.id, session, run, payload=published_payload
    )
    context = ToolContext(user_id=user.id, session_id=session.id, run_id=run.id, profile_name="session_analyst_v1")
    tool = ReadArtifactTool(db_session)

    # 进入新一轮 drafting：current_revision 指向新草稿 Revision。
    draft = await db_session.scalar(
        select(ArtifactDraft).where(ArtifactDraft.artifact_id == artifact.id)
    )
    assert draft is not None
    draft.status = "reviewing"
    draft.current_revision = 1
    draft_payload = {"title": "修订中", "data": {"overview": {"total_volume": 2}}}
    db_session.add(
        ArtifactDraftRevision(
            id=str(uuid4()),
            draft_id=draft.id,
            artifact_id=artifact.id,
            run_id=run.id,
            revision=1,
            schema_version="v1",
            payload_json=draft_payload,
            payload_hash="h" * 64,
            created_at=_now(),
        )
    )
    await db_session.flush()

    draft_read = _summary(await tool.execute(context, type(tool).input_model(artifact_id=artifact.id)))
    assert draft_read["status"] == "draft"
    assert draft_read["payload"] == draft_payload

    # 显式 version 恒读已发布 Version（语义不变）。
    versioned = _summary(
        await tool.execute(context, type(tool).input_model(artifact_id=artifact.id, version=1))
    )
    assert versioned["status"] == "published"
    assert versioned["payload"] == published_payload

    # Draft 释放（发布完成回 idle）后回到已发布 Version。
    draft.status = "idle"
    await db_session.flush()
    published_read = _summary(
        await tool.execute(context, type(tool).input_model(artifact_id=artifact.id))
    )
    assert published_read["status"] == "published"
    assert published_read["payload"] == published_payload


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
        assert json.loads(result.safe_summary)["returned_matches"] == 1
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


# ---------------------------------------------------------------------------
# P1-2: search_evidence / read_tool_result 消费统一有界模型视图
# ---------------------------------------------------------------------------


async def test_search_evidence_bounds_preview_not_full_5000_rows(
    db_session, user_factory
) -> None:
    """search_evidence 的 match 视图不得返回完整 5000 行（统一有界视图）。"""
    from app.agent_runtime.normalization import NormalizationRegistry

    user = await user_factory()
    session, run, _step, call = await _make_chain(db_session, user.id)
    rows = [{"关键词": f"美妆{i}", "声量": i} for i in range(5000)]
    payload = {"result": json.dumps({"rows": rows, "total": 5000})}
    writer = EvidenceWriter(db_session)
    await writer.write(
        session_id=session.id,
        run_id=run.id,
        tool_call_id=call.id,
        source_type="mcp",
        source_name="query_analysis_data",
        scope_json=None,
        period_json=None,
        raw_payload=payload,
        normalization=NormalizationRegistry().normalize("query_analysis_data", payload),
    )
    context = ToolContext(user_id=user.id, session_id=session.id, run_id=run.id, profile_name="session_analyst_v1")
    tool = SearchEvidenceTool(db_session)
    result = await tool.execute(context, type(tool).input_model(query="美妆0"))
    data = _summary(result)
    assert data["returned_matches"] >= 1
    view = data["matches"][0]["view"]
    # 归一化 preview 只有 200 行（_MAX_MODEL_ARRAY_ROWS），绝不含 5000 行。
    assert len(view["preview"]["rows"]) <= 200


async def test_read_tool_result_returns_pagination_and_unified_diagnostics(
    db_session, user_factory
) -> None:
    """read_tool_result 同时返回分页 items（有界）与统一 normalization 诊断。"""
    from app.agent_runtime.normalization import NormalizationRegistry

    user = await user_factory()
    session, run, _step, call = await _make_chain(db_session, user.id)
    payload = {"rows": [{"关键词": "美妆", "声量": i} for i in range(10)]}
    writer = EvidenceWriter(db_session)
    item = await writer.write(
        session_id=session.id,
        run_id=run.id,
        tool_call_id=call.id,
        source_type="mcp",
        source_name="query_analysis_data",
        scope_json=None,
        period_json=None,
        raw_payload=payload,
        normalization=NormalizationRegistry().normalize("query_analysis_data", payload),
    )
    context = ToolContext(user_id=user.id, session_id=session.id, run_id=run.id, profile_name="session_analyst_v1")
    tool = ReadToolResultTool(db_session)
    data = _summary(await tool.execute(context, type(tool).input_model(evidence_id=item.id, limit=200)))
    # 分页 items 返回原始行（有界），且长度受限。
    assert data["total"] == 10
    assert len(data["items"]) == 10
    assert data["items"][0]["关键词"] == "美妆"
    # 统一诊断来自模型视图。
    assert data["normalization_status"] == "incomplete"
    assert data["unmapped_fields"] == ["关键词"]


# ---------------------------------------------------------------------------
# remember_scope
# ---------------------------------------------------------------------------


async def _scope_entries(db_session, session_id: str) -> list[MemoryEntry]:
    return list(
        (
            await db_session.scalars(
                select(MemoryEntry)
                .where(
                    MemoryEntry.session_id == session_id,
                    MemoryEntry.memory_type == "confirmed_scope",
                )
                .order_by(MemoryEntry.created_at, MemoryEntry.id)
            )
        ).all()
    )


async def _add_user_message(db_session, session_id: str, run_id: str, content: str) -> AgentMessage:
    message = AgentMessage(
        id=str(uuid4()),
        session_id=session_id,
        run_id=run_id,
        role="user",
        content=content,
        sequence=1,
        created_at=_now(),
    )
    db_session.add(message)
    await db_session.flush()
    return message


async def test_remember_scope_writes_confirmed_scope_entries(db_session, user_factory) -> None:
    user = await user_factory()
    session, run, _step, _call = await _make_chain(db_session, user.id)
    message = await _add_user_message(db_session, session.id, run.id, "分析近30天小红书")
    context = ToolContext(
        user_id=user.id, session_id=session.id, run_id=run.id, profile_name="session_analyst_v1"
    )

    tool = RememberScopeTool(db_session)
    result = await tool.execute(
        context,
        RememberScopeArgs(
            domain="brand",
            values={"period": "近30天", "platform": "小红书"},
            source_message_id=message.id,
        ),
    )
    data = _summary(result)
    assert data["domain"] == "brand"
    assert data["remembered"] == {"period": "近30天", "platform": "小红书"}

    entries = await _scope_entries(db_session, session.id)
    assert len(entries) == 2
    by_field = {entry.content_json["field"]: entry for entry in entries}
    assert by_field["period"].content_json["value"] == "近30天"
    assert by_field["platform"].content_json["value"] == "小红书"
    for entry in entries:
        assert entry.content_json["domain"] == "brand"
        assert entry.content_json["explicit"] is True
        assert entry.content_json["source_message_id"] == message.id
        assert entry.source_run_id == run.id
        assert entry.superseded_at is None


async def test_remember_scope_supersedes_same_domain_field(db_session, user_factory) -> None:
    """同 domain+field 的旧 active 条目被 supersede；其他 field / domain 不受影响。"""
    user = await user_factory()
    session, run, _step, _call = await _make_chain(db_session, user.id)
    message = await _add_user_message(db_session, session.id, run.id, "分析近30天小红书")
    context = ToolContext(
        user_id=user.id, session_id=session.id, run_id=run.id, profile_name="session_analyst_v1"
    )
    tool = RememberScopeTool(db_session)

    first = await tool.execute(
        context,
        RememberScopeArgs(
            domain="brand",
            values={"period": "近30天", "platform": "小红书"},
            source_message_id=message.id,
        ),
    )
    assert first.status == "success"
    second = await tool.execute(
        context,
        RememberScopeArgs(
            domain="brand", values={"period": "近90天"}, source_message_id=message.id
        ),
    )
    data = _summary(second)
    assert data["superseded"] == 1

    entries = await _scope_entries(db_session, session.id)
    active = {
        entry.content_json["field"]: entry.content_json["value"]
        for entry in entries
        if entry.superseded_at is None
    }
    # period 被新值替代，platform 保持 active。
    assert active == {"period": "近90天", "platform": "小红书"}
    superseded = [entry for entry in entries if entry.superseded_at is not None]
    assert len(superseded) == 1
    assert superseded[0].content_json["value"] == "近30天"


async def test_remember_scope_invalid_domain_rejected(db_session, user_factory) -> None:
    user = await user_factory()
    session, run, _step, _call = await _make_chain(db_session, user.id)
    context = ToolContext(
        user_id=user.id, session_id=session.id, run_id=run.id, profile_name="session_analyst_v1"
    )
    tool = RememberScopeTool(db_session)
    result = await tool.execute(
        context,
        {"domain": "product", "values": {"period": "近30天"}, "source_message_id": "m-1"},
    )
    assert result.status == "failed"
    assert result.error_type == INVALID_ARGUMENTS
    assert await _scope_entries(db_session, session.id) == []


async def test_remember_scope_empty_values_rejected(db_session, user_factory) -> None:
    """空 values（无字段可确认）是模型幻觉：结构化失败，不落任何记忆。"""
    user = await user_factory()
    session, run, _step, _call = await _make_chain(db_session, user.id)
    message = await _add_user_message(db_session, session.id, run.id, "分析小红书")
    context = ToolContext(
        user_id=user.id, session_id=session.id, run_id=run.id, profile_name="session_analyst_v1"
    )
    tool = RememberScopeTool(db_session)
    result = await tool.execute(
        context,
        {"domain": "brand", "values": {}, "source_message_id": message.id},
    )
    assert result.status == "failed"
    assert result.error_type == INVALID_ARGUMENTS
    assert await _scope_entries(db_session, session.id) == []


async def test_remember_scope_source_message_ownership_validated(
    db_session, user_factory
) -> None:
    """source_message_id 必须存在、属本 Session 且为用户消息（Gate A 审查：
    不存在的消息、其他 Session 的消息、assistant 消息一律 not_found）。"""
    user = await user_factory()
    session, run, _step, _call = await _make_chain(db_session, user.id)
    other_session, other_run, _s2, _c2 = await _make_chain(db_session, user.id)
    other_message = await _add_user_message(
        db_session, other_session.id, other_run.id, "别的会话"
    )
    assistant = AgentMessage(
        id=str(uuid4()),
        session_id=session.id,
        run_id=run.id,
        role="assistant",
        content="请确认周期",
        sequence=2,
        created_at=_now(),
    )
    db_session.add(assistant)
    await db_session.flush()
    context = ToolContext(
        user_id=user.id, session_id=session.id, run_id=run.id, profile_name="session_analyst_v1"
    )
    tool = RememberScopeTool(db_session)

    for bad_id in (str(uuid4()), other_message.id, assistant.id):
        result = await tool.execute(
            context,
            RememberScopeArgs(
                domain="brand", values={"period": "近30天"}, source_message_id=bad_id
            ),
        )
        assert result.status == "failed"
        assert result.error_type == NOT_FOUND
    assert await _scope_entries(db_session, session.id) == []


async def test_remember_scope_cross_user_forbidden(db_session, user_factory) -> None:
    owner = await user_factory()
    other = await user_factory()
    session, run, _step, _call = await _make_chain(db_session, owner.id)
    message = await _add_user_message(db_session, session.id, run.id, "分析抖音")
    context = ToolContext(
        user_id=other.id, session_id=session.id, run_id=run.id, profile_name="session_analyst_v1"
    )
    tool = RememberScopeTool(db_session)
    result = await tool.execute(
        context,
        RememberScopeArgs(
            domain="kol", values={"platform": "抖音"}, source_message_id=message.id
        ),
    )
    assert result.status == "failed"
    assert result.error_type == FORBIDDEN
    assert await _scope_entries(db_session, session.id) == []


async def test_search_evidence_upload_source_no_storage_path(
    db_session, user_factory
) -> None:
    """upload Evidence 可搜索且不泄漏本地存储路径（Gate B Task 4）。

    matches 带 source_type=user_upload；safe_summary 绝不包含 storage_key
    或本地目录信息（storage path 只存在于服务端 agent_uploads 行）。
    """
    from app.agent_runtime.models import AgentUpload

    user = await user_factory()
    session, run, _step, call = await _make_chain(db_session, user.id)
    now = _now()
    upload = AgentUpload(
        id=str(uuid4()),
        user_id=user.id,
        session_id=session.id,
        original_filename="投放数据.csv",
        mime_type="text/csv",
        size_bytes=120,
        sha256="u" * 64,
        storage_key=f"secret-dir/{user.id}/abcd-{('u' * 16)}.csv",
        status="parsed",
        created_at=now,
        completed_at=now,
    )
    db_session.add(upload)
    await db_session.flush()
    writer = EvidenceWriter(db_session)
    item = await writer.write(
        session_id=session.id,
        run_id=None,
        tool_call_id=None,
        upload_id=upload.id,
        source_type="user_upload",
        source_name="user_upload",
        scope_json=None,
        period_json=None,
        raw_payload={"columns": ["平台", "声量"], "rows": [{"平台": "小红书", "声量": 100}]},
        collected_at=now,
    )
    context = ToolContext(
        user_id=user.id, session_id=session.id, run_id=run.id, profile_name="session_analyst_v1"
    )

    tool = SearchEvidenceTool(db_session)
    result = await tool.execute(context, type(tool).input_model(query="小红书"))
    data = _summary(result)
    matches = {m["evidence_id"]: m for m in data["matches"]}
    assert item.id in matches
    assert matches[item.id]["source_type"] == "user_upload"
    rendered = json.dumps(data, ensure_ascii=False)
    assert "secret-dir" not in rendered
    assert "storage_key" not in rendered
    assert "agent-uploads" not in rendered


# ---------------------------------------------------------------------------
# P1-2/P1-3: DataTap 真实包装分页 + 总响应字符预算
# ---------------------------------------------------------------------------


async def _read_all_rows(db_session, context, tool, evidence_id, limit=100) -> list[dict]:
    """按 cursor 遍历全部源 items，返回合并后的行列表。"""
    all_rows: list[dict] = []
    cursor = None
    while True:
        args = {"evidence_id": evidence_id, "limit": limit}
        if cursor is not None:
            args["cursor"] = int(cursor)
        data = _summary(await tool.execute(context, type(tool).input_model(**args)))
        all_rows.extend(data["items"])
        cursor = data["next_cursor"]
        if cursor is None:
            return all_rows, data


async def test_read_tool_result_paginates_wrapped_datatap_300_rows(
    db_session, user_factory
) -> None:
    """DataTap {result: '<json>'} 包装：300 行正确分页，could 不丢行不重复。"""
    user = await user_factory()
    session, run, _step, call = await _make_chain(db_session, user.id)
    payload = {"result": json.dumps({"rows": [{"volume": i} for i in range(300)]})}
    evidence_id = await _make_evidence(db_session, session, run, call, raw_payload=payload)
    context = ToolContext(user_id=user.id, session_id=session.id, run_id=run.id, profile_name="session_analyst_v1")
    tool = ReadToolResultTool(db_session)

    # 第一页
    first = _summary(await tool.execute(context, type(tool).input_model(evidence_id=evidence_id, limit=100)))
    assert first["total"] == 300
    assert first["next_cursor"] is not None
    assert len(first["items"]) == 100
    assert first["items"][0]["volume"] == 0
    assert first["items"][-1]["volume"] == 99

    # 中间页
    middle = _summary(await tool.execute(context, type(tool).input_model(evidence_id=evidence_id, cursor=100, limit=100)))
    assert middle["items"][0]["volume"] == 100
    assert middle["items"][-1]["volume"] == 199

    # 最后一页
    last = _summary(await tool.execute(context, type(tool).input_model(evidence_id=evidence_id, cursor=200, limit=100)))
    assert len(last["items"]) == 100
    assert last["items"][0]["volume"] == 200
    assert last["items"][-1]["volume"] == 299
    assert last["next_cursor"] is None

    # 全部 300 行不丢、不重复
    all_rows, _end = await _read_all_rows(db_session, context, tool, evidence_id, limit=100)
    assert len(all_rows) == 300
    assert len({row["volume"] for row in all_rows}) == 300
    assert sorted(row["volume"] for row in all_rows) == list(range(300))
    # 每页响应都合法 JSON
    for start in (0, 100, 200):
        page = _summary(await tool.execute(context, type(tool).input_model(evidence_id=evidence_id, cursor=start, limit=100)))
        json.loads(json.dumps(page))


async def test_read_tool_result_supports_other_container_keys(db_session, user_factory) -> None:
    """解包后支持 list/items/data/posts/records 等容器键。"""
    user = await user_factory()
    session, run, _step, call = await _make_chain(db_session, user.id)
    for key in ("list", "items", "data", "posts", "records"):
        payload = {key: [{"v": i} for i in range(5)]}
        evidence_id = await _make_evidence(db_session, session, run, call, raw_payload=payload)
        context = ToolContext(user_id=user.id, session_id=session.id, run_id=run.id, profile_name="session_analyst_v1")
        data = _summary(await ReadToolResultTool(db_session).execute(
            context, ReadToolResultTool.input_model(evidence_id=evidence_id, limit=10)
        ))
        assert data["total"] == 5, key
        assert len(data["items"]) == 5, key


async def test_read_tool_result_malformed_wrapped_payload_controlled(db_session, user_factory) -> None:
    """JSON 字符串解析失败返回受控单项结果，不抛 500。"""
    user = await user_factory()
    session, run, _step, call = await _make_chain(db_session, user.id)
    payload = {"result": "not-valid-json{{{"}
    evidence_id = await _make_evidence(db_session, session, run, call, raw_payload=payload)
    context = ToolContext(user_id=user.id, session_id=session.id, run_id=run.id, profile_name="session_analyst_v1")
    tool = ReadToolResultTool(db_session)
    data = _summary(await tool.execute(context, type(tool).input_model(evidence_id=evidence_id)))
    assert data["total"] == 1
    assert len(data["items"]) == 1
    json.loads(json.dumps(data))


async def test_read_tool_result_total_budget_caps_items_and_cursor_advances(
    db_session, user_factory
) -> None:
    """200 个近最大 item 合并超预算：逐页返回、cursor 精确推进、最终能遍历全部。"""
    user = await user_factory()
    session, run, _step, call = await _make_chain(db_session, user.id)
    payload = {"rows": [{"idx": i, "text": "x" * 900} for i in range(200)]}
    evidence_id = await _make_evidence(db_session, session, run, call, raw_payload=payload)
    context = ToolContext(user_id=user.id, session_id=session.id, run_id=run.id, profile_name="session_analyst_v1")
    tool = ReadToolResultTool(db_session)

    first = _summary(await tool.execute(context, type(tool).input_model(evidence_id=evidence_id, limit=200)))
    # 预算截断：不可能一次返回全部 200 个近最大 item。
    assert len(first["items"]) < 200
    assert len(json.dumps(first)) <= _MAX_TOOL_RESULT_TOTAL_CHARS
    assert first["next_cursor"] is not None

    # 遍历全部源 items，不丢不重。
    all_rows, _end = await _read_all_rows(db_session, context, tool, evidence_id, limit=200)
    assert len(all_rows) == 200
    assert len({row["idx"] for row in all_rows}) == 200
    assert sorted(row["idx"] for row in all_rows) == list(range(200))


async def test_read_tool_result_single_oversized_item_placeholder(db_session, user_factory) -> None:
    """单个超大 item（有界后仍超预算）返回占位并推进 cursor，不无限循环。"""
    user = await user_factory()
    session, run, _step, call = await _make_chain(db_session, user.id)
    # 200 字段 × 2000 字符长字符串 → 有界后仍 ~200KB，超过 50KB 总预算。
    huge = {f"k{i}": "长" * 2000 for i in range(200)}
    payload = {"rows": [huge]}
    evidence_id = await _make_evidence(db_session, session, run, call, raw_payload=payload)
    context = ToolContext(user_id=user.id, session_id=session.id, run_id=run.id, profile_name="session_analyst_v1")
    tool = ReadToolResultTool(db_session)

    data = _summary(await tool.execute(context, type(tool).input_model(evidence_id=evidence_id, limit=10)))
    assert data["items"] == [{"__truncated__": True, "__reason__": "item_exceeds_model_budget"}]
    assert data["truncated"] is True
    # cursor 已越过该超大 item（total=1 → 无下一页），不会无限循环。
    assert data["next_cursor"] is None
    assert len(json.dumps(data)) <= _MAX_TOOL_RESULT_TOTAL_CHARS


async def test_search_evidence_total_budget_limits_large_views(db_session, user_factory) -> None:
    """search_evidence 多个大 view 仍满足总预算；超匹配上限时截断并标记；
    完整 view 放不下的 match 退化为最小 match（含 evidence_id）。"""
    user = await user_factory()
    session, run, _step, call = await _make_chain(db_session, user.id)
    for _ in range(25):
        big_payload = {"rows": [{"idx": j, "text": "x" * 800} for j in range(200)]}
        await _make_evidence(db_session, session, run, call, raw_payload=big_payload)
    context = ToolContext(user_id=user.id, session_id=session.id, run_id=run.id, profile_name="session_analyst_v1")
    tool = SearchEvidenceTool(db_session)
    result = await tool.execute(context, type(tool).input_model(query=""))
    data = _summary(result)
    # 与真实响应同构（ensure_ascii=False）测量总字符。
    assert len(json.dumps(data, ensure_ascii=False)) <= _MAX_TOOL_RESULT_TOTAL_CHARS
    # 匹配上限 20：返回 20 个（完整 view 放不下的退化为最小 match），其余标记 has_more。
    assert data["returned_matches"] == 20
    assert data["has_more"] is True
    assert data["truncated"] is True
    assert data["scanned_count"] >= 20
    # 每个 match 都有 evidence_id，模型可 read_tool_result 钻取。
    assert all(m.get("evidence_id") for m in data["matches"])
    json.loads(json.dumps(data, ensure_ascii=False))


# ---------------------------------------------------------------------------
# P1 硬预算：read_tool_result / search_evidence 整个响应 <= 50KB
# ---------------------------------------------------------------------------


async def _make_evidence_with_diagnostics(
    db_session, session, run, call, *, rows: list[dict], mapping: dict, unmapped: list
) -> str:
    """写入带超大诊断字段的 Evidence（field_mapping/unmapped_fields 巨大）。"""
    from app.agent_runtime.normalization import NormalizationResult

    writer = EvidenceWriter(db_session)
    normalization = NormalizationResult(
        version="normalization_v1",
        status="normalized",
        preview={"rows": rows, "row_count": len(rows), "truncated": False},
        field_mapping=mapping,
        unmapped_fields=tuple(unmapped),
        truncated=False,
    )
    item = await writer.write(
        session_id=session.id,
        run_id=run.id,
        tool_call_id=call.id,
        source_type="mcp",
        source_name="query_analysis_data",
        scope_json=None,
        period_json=None,
        raw_payload={"rows": rows},
        normalization=normalization,
    )
    return item.id


async def test_read_tool_result_huge_diagnostics_within_budget(
    db_session, user_factory
) -> None:
    """Evidence 诊断字段本身超过预算：safe_summary <= 50KB、JSON 合法、
    多页遍历无丢行、无重复、cursor 单调前进。"""
    user = await user_factory()
    session, run, _step, call = await _make_chain(db_session, user.id)
    rows = [{"volume": i} for i in range(50)]
    evidence_id = await _make_evidence_with_diagnostics(
        db_session, session, run, call,
        rows=rows,
        mapping={f"col{i}": "x" * 900 for i in range(200)},
        unmapped=["x" * 900] * 300,
    )
    context = ToolContext(user_id=user.id, session_id=session.id, run_id=run.id, profile_name="session_analyst_v1")
    tool = ReadToolResultTool(db_session)

    first = _summary(await tool.execute(context, type(tool).input_model(evidence_id=evidence_id, limit=20)))
    assert len(json.dumps(first, ensure_ascii=False)) <= _MAX_TOOL_RESULT_TOTAL_CHARS
    json.loads(json.dumps(first, ensure_ascii=False))

    all_rows: list[int] = [
        item["volume"] for item in first["items"] if isinstance(item, dict) and "volume" in item
    ]
    cursor = first["next_cursor"]
    prev_cursor = 0
    while cursor is not None:
        page = _summary(await tool.execute(
            context, type(tool).input_model(evidence_id=evidence_id, cursor=int(cursor), limit=20)
        ))
        assert len(json.dumps(page, ensure_ascii=False)) <= _MAX_TOOL_RESULT_TOTAL_CHARS
        json.loads(json.dumps(page, ensure_ascii=False))
        # 提取实际数据行（跳过截断占位）。
        all_rows.extend(item["volume"] for item in page["items"] if isinstance(item, dict) and "volume" in item)
        assert int(cursor) > prev_cursor  # cursor 单调前进
        prev_cursor = int(cursor)
        cursor = page["next_cursor"]
    assert len(all_rows) == 50
    assert len(set(all_rows)) == 50
    assert sorted(all_rows) == list(range(50))


async def test_search_evidence_long_query_structured_invalid(
    db_session, user_factory
) -> None:
    """超长 query 返回结构化 invalid_arguments，不能 500、不能原样回显 60KB。"""
    user = await user_factory()
    session, run, _step, call = await _make_chain(db_session, user.id)
    await _make_evidence(db_session, session, run, call, raw_payload={"rows": [{"k": 1}]})
    context = ToolContext(user_id=user.id, session_id=session.id, run_id=run.id, profile_name="session_analyst_v1")
    tool = SearchEvidenceTool(db_session)
    # 以 dict 传入，让 execute 内 _parse_args 走校验（构造 input_model 会提前抛）。
    result = await tool.execute(context, {"query": "x" * 60_000})
    assert result.status == "failed"
    assert result.error_type == INVALID_ARGUMENTS
    assert len(result.safe_summary) < 1000  # 不原样回显 60KB


# ---------------------------------------------------------------------------
# 阻断 1: search_evidence keyset 分页（超过 500 条的 Session 可继续搜索）
# ---------------------------------------------------------------------------


async def _seed_many_evidence(
    db_session, session, run, call, count: int, *, target_index: int | None = None
) -> str:
    """写入 count 条 Evidence（collected_at 递增）；target_index 处 source_name 为
    target_tool（其余 other_tool）。返回 target evidence_id 或 ""。"""
    from datetime import timedelta

    base = utc_now() - timedelta(days=count)
    target_id = ""
    for i in range(count):
        source_name = "target_tool" if i == target_index else "other_tool"
        item = await EvidenceWriter(db_session).write(
            session_id=session.id,
            run_id=run.id,
            tool_call_id=call.id,
            source_type="mcp",
            source_name=source_name,
            scope_json={"brand": "美妆"},
            period_json=None,
            raw_payload={"rows": [{"keyword": source_name, "volume": i}]},
            collected_at=base + timedelta(minutes=i),
        )
        if i == target_index:
            target_id = item.id
    return target_id


async def test_search_evidence_paginates_beyond_500(
    db_session, user_factory
) -> None:
    """600 条 Evidence、目标只在第 501 条之后：第一页 returned=0 + has_more，
    使用 next_cursor 后能找到目标。"""
    user = await user_factory()
    session, run, _step, call = await _make_chain(db_session, user.id)
    target_id = await _seed_many_evidence(db_session, session, run, call, 600, target_index=0)
    assert target_id
    context = ToolContext(user_id=user.id, session_id=session.id, run_id=run.id, profile_name="session_analyst_v1")
    tool = SearchEvidenceTool(db_session)

    first = _summary(await tool.execute(context, type(tool).input_model(query="target_tool")))
    assert first["returned_matches"] == 0
    assert first["has_more"] is True
    assert first["next_cursor"] is not None
    assert first["scanned_count"] == 500

    second = _summary(await tool.execute(
        context, type(tool).input_model(query="target_tool", cursor=first["next_cursor"])
    ))
    assert second["returned_matches"] == 1
    assert second["matches"][0]["evidence_id"] == target_id
    assert second["has_more"] is False


async def test_search_evidence_paginates_all_600_no_loss(
    db_session, user_factory
) -> None:
    """600 条全部匹配：逐页遍历覆盖全部，id 不丢不重，cursor 单调推进。"""
    user = await user_factory()
    session, run, _step, call = await _make_chain(db_session, user.id)
    await _seed_many_evidence(db_session, session, run, call, 600)
    context = ToolContext(user_id=user.id, session_id=session.id, run_id=run.id, profile_name="session_analyst_v1")
    tool = SearchEvidenceTool(db_session)

    all_ids: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        args: dict = {"query": ""}
        if cursor is not None:
            args["cursor"] = cursor
        data = _summary(await tool.execute(context, type(tool).input_model(**args)))
        assert len(json.dumps(data, ensure_ascii=False)) <= _MAX_TOOL_RESULT_TOTAL_CHARS
        all_ids.extend(m["evidence_id"] for m in data["matches"])
        pages += 1
        assert pages <= 200  # 防死循环
        if data["next_cursor"] is None:
            break
        assert data["next_cursor"] != cursor  # cursor 单调推进
        cursor = data["next_cursor"]
    assert len(all_ids) == 600
    assert len(set(all_ids)) == 600


async def test_search_evidence_empty_page_has_more_and_advances(
    db_session, user_factory
) -> None:
    """页面没有匹配但后面仍有数据：returned=0、has_more=true、cursor 仍推进。"""
    user = await user_factory()
    session, run, _step, call = await _make_chain(db_session, user.id)
    await _seed_many_evidence(db_session, session, run, call, 600)
    context = ToolContext(user_id=user.id, session_id=session.id, run_id=run.id, profile_name="session_analyst_v1")
    tool = SearchEvidenceTool(db_session)

    first = _summary(await tool.execute(context, type(tool).input_model(query="zzz-no-match")))
    assert first["returned_matches"] == 0
    assert first["has_more"] is True
    assert first["next_cursor"] is not None

    second = _summary(await tool.execute(
        context, type(tool).input_model(query="zzz-no-match", cursor=first["next_cursor"])
    ))
    assert second["returned_matches"] == 0
    assert second["has_more"] is False
    assert second["next_cursor"] is None


async def test_search_evidence_budget_stop_does_not_skip_matches(
    db_session, user_factory
) -> None:
    """响应预算在某个 match 前耗尽：next_cursor 不能越过未返回 match，下一页能
    返回该 match（逐页遍历 40 个中等 view 全部无丢失）。"""
    user = await user_factory()
    session, run, _step, call = await _make_chain(db_session, user.id)
    for i in range(40):
        payload = {"rows": [{"idx": j, "text": "x" * 100} for j in range(60)]}
        await _make_evidence(db_session, session, run, call, raw_payload=payload, source_name=f"mid{i}")
    context = ToolContext(user_id=user.id, session_id=session.id, run_id=run.id, profile_name="session_analyst_v1")
    tool = SearchEvidenceTool(db_session)

    all_ids: list[str] = []
    cursor: str | None = None
    pages = 0
    first_returned: int | None = None
    while True:
        args: dict = {"query": ""}
        if cursor is not None:
            args["cursor"] = cursor
        data = _summary(await tool.execute(context, type(tool).input_model(**args)))
        assert len(json.dumps(data, ensure_ascii=False)) <= _MAX_TOOL_RESULT_TOTAL_CHARS
        if first_returned is None:
            first_returned = data["returned_matches"]
        all_ids.extend(m["evidence_id"] for m in data["matches"])
        pages += 1
        assert pages <= 100
        if data["next_cursor"] is None:
            break
        assert data["next_cursor"] != cursor
        cursor = data["next_cursor"]
    # 第一页受预算限制未返回全部，但遍历后 40 条全部出现且不重复。
    assert first_returned is not None and first_returned < 40
    assert len(all_ids) == 40
    assert len(set(all_ids)) == 40


async def test_search_evidence_minimal_match_marks_view_omitted(
    db_session, user_factory
) -> None:
    """完整 view 降级为最小 stub：view_omitted=true、view_truncated=true、
    truncated=true；has_more 只表示是否还有未扫描 Evidence。"""
    from datetime import timedelta

    user = await user_factory()
    session, run, _step, call = await _make_chain(db_session, user.id)
    huge_id = await _make_evidence_with_diagnostics(
        db_session, session, run, call,
        rows=[{"volume": i} for i in range(3)],
        mapping={f"col{i}": "x" * 900 for i in range(200)},
        unmapped=["x" * 900] * 300,
    )
    # small 显式更新（collected_at 更晚 → DESC 顺序在前），让 huge 的完整 view
    # 因预算放不下而退化为最小 stub。
    small = await EvidenceWriter(db_session).write(
        session_id=session.id,
        run_id=run.id,
        tool_call_id=call.id,
        source_type="mcp",
        source_name="query_analysis_data",
        scope_json=None,
        period_json=None,
        raw_payload={"rows": [{"k": 1}]},
        collected_at=utc_now() + timedelta(seconds=1),
    )
    small_id = small.id
    context = ToolContext(user_id=user.id, session_id=session.id, run_id=run.id, profile_name="session_analyst_v1")
    tool = SearchEvidenceTool(db_session)
    data = _summary(await tool.execute(context, type(tool).input_model(query="")))
    assert data["returned_matches"] == 2
    matches_by_id = {m["evidence_id"]: m for m in data["matches"]}
    # 小 view 完整返回；大 view 因预算降级为最小 stub。
    assert "view" in matches_by_id[small_id]
    huge = matches_by_id[huge_id]
    assert huge["view_omitted"] is True
    assert "view" not in huge
    for key in ("evidence_id", "source_type", "source_name", "run_id", "collected_at"):
        assert key in huge
    assert data["view_truncated"] is True
    assert data["truncated"] is True
    assert data["has_more"] is False  # 只有 2 条，没有未扫描 Evidence
    assert len(json.dumps(data, ensure_ascii=False)) <= _MAX_TOOL_RESULT_TOTAL_CHARS


async def test_search_evidence_invalid_cursor_structured_failure(
    db_session, user_factory
) -> None:
    """非法/超长 cursor：invalid_arguments，不抛 500，不泄露其他 Session 数据。"""
    user = await user_factory()
    session, run, _step, call = await _make_chain(db_session, user.id)
    await _make_evidence(db_session, session, run, call, raw_payload={"rows": [{"k": 1}]})
    context = ToolContext(user_id=user.id, session_id=session.id, run_id=run.id, profile_name="session_analyst_v1")
    tool = SearchEvidenceTool(db_session)

    for bad_cursor in ("garbage!!", "not-a-cursor", "x" * 500):
        result = await tool.execute(context, {"query": "", "cursor": bad_cursor})
        assert result.status == "failed"
        assert result.error_type == INVALID_ARGUMENTS, bad_cursor

    # 跨用户伪造 cursor：会话归属校验先于 cursor 解析，仍 404（不泄露存在性）。
    other = await user_factory()
    foreign = await tool.execute(
        ToolContext(user_id=other.id, session_id=session.id, run_id="r", profile_name="session_analyst_v1"),
        {"query": "", "cursor": "garbage"},
    )
    assert foreign.status == "failed"
    assert foreign.error_type in (NOT_FOUND, FORBIDDEN)


async def test_search_evidence_tampered_cursor_rejected(
    db_session, user_factory
) -> None:
    """合法 cursor 追加 !! 后必须 invalid_arguments（严格 Base64 + 重新编码校验）。"""
    user = await user_factory()
    session, run, _step, call = await _make_chain(db_session, user.id)
    await _seed_many_evidence(db_session, session, run, call, 600)
    context = ToolContext(user_id=user.id, session_id=session.id, run_id=run.id, profile_name="session_analyst_v1")
    tool = SearchEvidenceTool(db_session)

    first = _summary(await tool.execute(context, type(tool).input_model(query="")))
    assert first["next_cursor"] is not None
    valid_cursor = first["next_cursor"]

    # 追加 '!!'：lenient base64 会忽略，严格校验必须拒绝。
    tampered = valid_cursor + "!!"
    result = await tool.execute(context, {"query": "", "cursor": tampered})
    assert result.status == "failed"
    assert result.error_type == INVALID_ARGUMENTS

    # 追加合法 base64 字符（仍改变原始载荷）也必须拒绝（重新编码不一致）。
    tampered2 = valid_cursor + "AA"
    result2 = await tool.execute(context, {"query": "", "cursor": tampered2})
    assert result2.status == "failed"
    assert result2.error_type == INVALID_ARGUMENTS
