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
    assert data["total_matches"] >= 1
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
