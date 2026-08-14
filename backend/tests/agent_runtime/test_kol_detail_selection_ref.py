"""kol_detail selection 引用消费（v3 加固设计 §6.4）。

``POST /sessions/{id}/kol-details`` 透传的 ``selection_artifact_id`` /
``selection_version`` 此前被静默丢弃。本文件覆盖修复后的契约：

1. 归属校验：selection_artifact_id 必须是**当前 Session** 内
   ``kol_selection_v3`` Artifact；selection_version 必须指向该 Artifact 的
   已发布 Version；任何失败统一按归属失败处理（服务层抛
   ``KolDetailSelectionRefNotFound``，API 层 404，不泄漏资源存在性）；
2. 消费：解析后的名单 Version 进入 Run 的 ``prompt_snapshot_json``
   （``kol_detail.selection_version_id``，G3 恢复锚点同快照）、kol-detail
   Artifact 稳定行的 ``parent_artifact_id``、Draft Revision / 已发布
   Version 的 ``parent_artifact_version_id``（服务端权威绑定，不依赖模型
   传参），以及发给模型的触发消息文本（名单上下文）。
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.agent_artifacts.models import AgentArtifact, AgentArtifactVersion
from app.agent_artifacts.service import ArtifactService
from app.agent_runtime.kol_detail import (
    KolDetailSelectionRefNotFound,
    KolDetailRunService,
)
from app.agent_runtime.models import AgentRun
from app.agent_runtime.repository import utc_now

from tests.agent_artifacts.test_kol_analysis_builder import (
    _selection_item,
    _selection_payload,
)
from tests.agent_runtime.test_kol_detail import (
    KOL_UID,
    PLATFORM,
    T0,
    _cache_state,
    _make_actions,
    _make_evidence,
    _make_service,
    _make_session,
)


async def _make_published_selection(
    db, user_id: str, session_id: str
) -> tuple[AgentArtifact, AgentArtifactVersion]:
    """在当前 Session 落一个已发布的 kol_selection_v3 Artifact + Version v1。"""
    now = utc_now()
    run = AgentRun(
        id=str(uuid4()),
        session_id=session_id,
        user_id=user_id,
        run_kind="internal",
        visibility="internal",
        profile_name="selection_chain",
        profile_version="v1",
        model="test-model",
        status="completed",
        started_at=now,
    )
    db.add(run)
    await db.flush()
    scope = {
        "brand": "某品牌",
        "category": "美食",
        "platforms": ["小红书"],
        "audience": {"regions": [], "age_ranges": [], "interests": []},
        "filters": {},
    }
    artifact, _draft, revision = await ArtifactService(db).create_or_get_draft(
        session_id=session_id,
        user_id=user_id,
        run_id=run.id,
        module="kol-selection",
        business_fields={"scope": scope},
        schema_version="kol_selection_v3",
        payload=_selection_payload([_selection_item(KOL_UID)]),
        evidence_refs=[],
        artifact_type="kol_selection_v3",
    )
    version = AgentArtifactVersion(
        id=str(uuid4()),
        artifact_id=artifact.id,
        version=1,
        source_run_id=run.id,
        source_draft_revision_id=revision.id,
        parent_artifact_version_id=None,
        schema_version="kol_selection_v3",
        payload_json=revision.payload_json,
        evidence_refs_json=[],
        review_json=None,
        data_status="complete",
        created_at=now,
    )
    db.add(version)
    artifact.status = "published"
    artifact.latest_version = 1
    await db.flush()
    return artifact, version


# ---------------------------------------------------------------------------
# 1. 消费链路：快照 / Artifact parent / Revision·Version 绑定 / prompt
# ---------------------------------------------------------------------------


async def test_selection_ref_consumed_into_snapshot_parent_and_prompt(
    db_session, user_factory
) -> None:
    """合法名单引用：进入 prompt_snapshot（含解析后的 selection_version_id）、
    kol-detail 稳定行 parent_artifact_id、已发布 Version 的
    parent_artifact_version_id（服务端权威绑定，模型未传 parent 参数），
    并出现在发给模型的触发消息中。"""
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    sel_artifact, sel_version = await _make_published_selection(
        db_session, user.id, session.id
    )
    evidence = await _make_evidence(db_session, user.id, session.id)
    gateway, service = _make_service(
        db_session,
        actions=_make_actions(db_session, evidence, _cache_state()),
        evidence=evidence,
        now_fn=lambda: T0,
    )

    summary = await service.create(
        user.id,
        session.id,
        PLATFORM,
        KOL_UID,
        selection_artifact_id=sel_artifact.id,
        selection_version="1",
    )

    assert summary.cached is False
    assert summary.run_id is not None

    # 1) prompt_snapshot：透传字段 + 解析后的已发布 Version id（G3 恢复锚点）。
    run_row = await db_session.get(AgentRun, summary.run_id)
    assert run_row is not None
    trigger = (run_row.prompt_snapshot_json or {}).get("kol_detail") or {}
    assert trigger.get("selection_artifact_id") == sel_artifact.id
    assert trigger.get("selection_version") == "1"
    assert trigger.get("selection_version_id") == sel_version.id

    # 2) Artifact parent：稳定行只记 parent_artifact_id。
    detail_artifact = await db_session.scalar(
        select(AgentArtifact).where(
            AgentArtifact.session_id == session.id,
            AgentArtifact.artifact_key == f"kol-detail:{PLATFORM}:{KOL_UID}",
        )
    )
    assert detail_artifact is not None
    assert detail_artifact.parent_artifact_id == sel_artifact.id

    # 3) 版本绑定：发布 Version 的 parent_artifact_version_id 指向名单 Version
    #    （模型的 create_draft 未传 parent 参数——绑定由服务端完成）。
    published = await db_session.scalar(
        select(AgentArtifactVersion).where(
            AgentArtifactVersion.source_run_id == summary.run_id
        )
    )
    assert published is not None
    assert published.parent_artifact_version_id == sel_version.id

    # 4) prompt：发给模型的触发消息包含名单上下文。
    first_messages = gateway.calls[0]["messages"]
    trigger_message = next(m for m in first_messages if m.role == "user")
    assert sel_artifact.id in trigger_message.content


# ---------------------------------------------------------------------------
# 2. 归属校验失败（统一 KolDetailSelectionRefNotFound → API 404）
# ---------------------------------------------------------------------------


async def test_selection_ref_unknown_artifact_rejected(db_session, user_factory) -> None:
    """不存在的 selection_artifact_id：拒绝，且不创建任何 kol_detail Run。"""
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    service = KolDetailRunService(db_session, engine=None, now_fn=lambda: T0)

    with pytest.raises(KolDetailSelectionRefNotFound):
        await service.create(
            user.id,
            session.id,
            PLATFORM,
            KOL_UID,
            selection_artifact_id=str(uuid4()),
            selection_version="1",
        )
    count = await db_session.scalar(
        select(func.count(AgentRun.id)).where(AgentRun.session_id == session.id)
    )
    assert count == 0


async def test_selection_ref_cross_session_rejected(db_session, user_factory) -> None:
    """名单 Artifact 属于另一个 Session（同用户）：按归属失败拒绝，不泄漏存在性。"""
    user = await user_factory()
    session_a = await _make_session(db_session, user.id)
    session_b = await _make_session(db_session, user.id)
    sel_artifact, _ = await _make_published_selection(db_session, user.id, session_b.id)
    service = KolDetailRunService(db_session, engine=None, now_fn=lambda: T0)

    with pytest.raises(KolDetailSelectionRefNotFound):
        await service.create(
            user.id,
            session_a.id,
            PLATFORM,
            KOL_UID,
            selection_artifact_id=sel_artifact.id,
            selection_version="1",
        )


async def test_selection_ref_wrong_artifact_type_rejected(db_session, user_factory) -> None:
    """同 Session 但非 kol_selection_v3 的 Artifact（如 kol_detail_v2）：拒绝。"""
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    now = utc_now()
    other = AgentArtifact(
        id=str(uuid4()),
        session_id=session.id,
        user_id=user.id,
        module="kol-detail",
        artifact_type="kol_detail_v2",
        artifact_key="kol-detail:xiaohongshu:k9",
        status="published",
        latest_version=1,
        activity_sequence=0,
        created_at=now,
        updated_at=now,
    )
    db_session.add(other)
    await db_session.flush()
    service = KolDetailRunService(db_session, engine=None, now_fn=lambda: T0)

    with pytest.raises(KolDetailSelectionRefNotFound):
        await service.create(
            user.id,
            session.id,
            PLATFORM,
            KOL_UID,
            selection_artifact_id=other.id,
            selection_version="1",
        )


async def test_selection_ref_unpublished_version_rejected(db_session, user_factory) -> None:
    """名单 Artifact 存在但所指 Version 未发布（无该 Version 行）：拒绝。"""
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    sel_artifact, _ = await _make_published_selection(db_session, user.id, session.id)
    service = KolDetailRunService(db_session, engine=None, now_fn=lambda: T0)

    # 只发布了 v1，引用 v2 → 拒绝。
    with pytest.raises(KolDetailSelectionRefNotFound):
        await service.create(
            user.id,
            session.id,
            PLATFORM,
            KOL_UID,
            selection_artifact_id=sel_artifact.id,
            selection_version="2",
        )
    # 非数字版本号同样拒绝。
    with pytest.raises(KolDetailSelectionRefNotFound):
        await service.create(
            user.id,
            session.id,
            PLATFORM,
            KOL_UID,
            selection_artifact_id=sel_artifact.id,
            selection_version="abc",
        )


async def test_selection_ref_partial_rejected(db_session, user_factory) -> None:
    """只给一个字段（artifact_id 或 version 缺一）：引用不完整，拒绝。"""
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    sel_artifact, _ = await _make_published_selection(db_session, user.id, session.id)
    service = KolDetailRunService(db_session, engine=None, now_fn=lambda: T0)

    with pytest.raises(KolDetailSelectionRefNotFound):
        await service.create(
            user.id,
            session.id,
            PLATFORM,
            KOL_UID,
            selection_artifact_id=sel_artifact.id,
        )
    with pytest.raises(KolDetailSelectionRefNotFound):
        await service.create(
            user.id,
            session.id,
            PLATFORM,
            KOL_UID,
            selection_version="1",
        )


async def test_selection_ref_validated_even_on_cache_hit(db_session, user_factory) -> None:
    """缓存命中路径同样先校验引用：引用非法时按归属失败拒绝（不命中缓存）。"""
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    evidence = await _make_evidence(db_session, user.id, session.id)
    _, service = _make_service(
        db_session,
        actions=_make_actions(db_session, evidence, _cache_state()),
        evidence=evidence,
        now_fn=lambda: T0,
    )
    first = await service.create(user.id, session.id, PLATFORM, KOL_UID)
    assert first.cached is False  # 缓存已回填

    with pytest.raises(KolDetailSelectionRefNotFound):
        await service.create(
            user.id,
            session.id,
            PLATFORM,
            KOL_UID,
            selection_artifact_id=str(uuid4()),
            selection_version="1",
        )


async def test_valid_ref_on_cache_hit_serves_normally(db_session, user_factory) -> None:
    """缓存命中 + 合法引用：正常命中（校验通过不阻断缓存路径）。"""
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    sel_artifact, _ = await _make_published_selection(db_session, user.id, session.id)
    evidence = await _make_evidence(db_session, user.id, session.id)
    gateway, service = _make_service(
        db_session,
        actions=_make_actions(db_session, evidence, _cache_state()),
        evidence=evidence,
        now_fn=lambda: T0,
    )
    first = await service.create(user.id, session.id, PLATFORM, KOL_UID)
    assert first.cached is False
    calls_after_first = len(gateway.calls)

    hit = await service.create(
        user.id,
        session.id,
        PLATFORM,
        KOL_UID,
        selection_artifact_id=sel_artifact.id,
        selection_version="1",
    )
    assert hit.cached is True
    assert len(gateway.calls) == calls_after_first


# ---------------------------------------------------------------------------
# 3. 崩溃恢复：transcript 重建的触发上下文同样携带名单引用
# ---------------------------------------------------------------------------


def test_trigger_content_includes_selection_context() -> None:
    """触发消息锚点（首次启动与恢复共用）：携带名单引用时包含名单上下文。"""
    from app.agent_runtime.kol_detail import kol_detail_trigger_content

    content = kol_detail_trigger_content(
        PLATFORM, KOL_UID, selection_artifact_id="sel-1", selection_version="3"
    )
    assert PLATFORM in content
    assert KOL_UID in content
    assert "sel-1" in content
    # 无名单引用时保持原锚点文本（不追加名单上下文）。
    plain = kol_detail_trigger_content(PLATFORM, KOL_UID)
    assert "sel-1" not in plain
    assert plain != content


async def test_takeover_trigger_restores_selection_context(db_session, user_factory) -> None:
    """崩溃接管：transcript 从 prompt_snapshot 重建的触发消息保留名单上下文。"""
    from app.agent_runtime.kol_detail import (
        build_kol_detail_prompt_snapshot,
        kol_detail_trigger_content,
    )
    from app.agent_runtime.transcript import RunTranscriptLoader

    user = await user_factory()
    session = await _make_session(db_session, user.id)
    sel_artifact, sel_version = await _make_published_selection(
        db_session, user.id, session.id
    )
    now = utc_now()
    run = AgentRun(
        id=str(uuid4()),
        session_id=session.id,
        user_id=user.id,
        input_message_id=None,
        run_kind="user",
        visibility="user",
        profile_name="kol_detail_v1",
        profile_version="v1",
        model="test-model",
        prompt_snapshot_json=build_kol_detail_prompt_snapshot(
            platform=PLATFORM,
            kol_uid=KOL_UID,
            selection_artifact_id=sel_artifact.id,
            selection_version="1",
            selection_version_id=sel_version.id,
        ),
        status="running",
        decision_count=0,
        review_count=0,
        revision_count=0,
        started_at=now,
        lease_owner="dead-worker",
        lease_expires_at=now,
    )
    db_session.add(run)
    await db_session.flush()

    transcript = await RunTranscriptLoader(db_session).load(run)
    assert transcript.user_question == kol_detail_trigger_content(
        PLATFORM,
        KOL_UID,
        selection_artifact_id=sel_artifact.id,
        selection_version="1",
    )
    assert sel_artifact.id in transcript.user_question
