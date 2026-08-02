"""未读水位（artifact_read_states）测试（设计文档 §8.1 ReadState / Task 12）。

覆盖：
1. 模块最新 artifact_events.sequence > last_seen_sequence 时未读；标记已读后消除；
2. last_seen_sequence 只前进到前端已渲染的 sequence（max(old, new)）；
3. 更小的（stale）sequence 写入不会让水位倒退；
4. 未读按 (user, session, module) 隔离；
5. 兼容遗留写入方：遗留 module_key 行（module 为 NULL）被 UPSERT 更新而非撞唯一约束。
"""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, select

from app.agent_artifacts.models import AgentArtifactReadState
from app.agent_artifacts.service import ArtifactService
from app.artifacts.models import ArtifactReadState


async def _draft_brand(db_session, user, session, run, service, brand="瑞幸"):
    artifact, draft, revision = await service.create_or_get_draft(
        session_id=session.id,
        user_id=user.id,
        run_id=run.id,
        module="brand",
        business_fields={"brand": brand},
        schema_version="brand_report_v3",
        payload={"data": {"overview": {"total_volume": 100}}},
        evidence_refs=[],
        artifact_type="brand_report_v3",
    )
    return artifact, draft, revision


async def test_unread_until_marked_seen(
    db_session, user_factory, session_factory, run_factory, legacy_session_factory,
):
    user = await user_factory()
    session = await session_factory(user.id)
    await legacy_session_factory(user.id, session.id)
    run = await run_factory(session.id, user.id)
    service = ArtifactService(db_session)

    artifact, draft, _ = await _draft_brand(db_session, user, session, run, service)

    # 最新事件 sequence=1 > 0（无读水位）→ 未读
    assert await service.get_unread(user.id, session.id, "brand") is True

    # 标记已读（水位到 1）→ 已读
    assert await service.advance_read_state(user.id, session.id, "brand", 1) == 1
    assert await service.get_unread(user.id, session.id, "brand") is False

    # 新 Draft 更新产生 sequence=2 → 再次未读
    await service.update_draft(
        run_id=run.id, draft_id=draft.id, payload={"data": {"overview": {"x": 2}}},
        evidence_refs=[],
    )
    assert await service.get_unread(user.id, session.id, "brand") is True
    await service.advance_read_state(user.id, session.id, "brand", 2)
    assert await service.get_unread(user.id, session.id, "brand") is False


async def test_read_state_watermark_only_advances_and_never_backwards(
    db_session, user_factory, session_factory, run_factory, legacy_session_factory,
):
    user = await user_factory()
    session = await session_factory(user.id)
    await legacy_session_factory(user.id, session.id)
    run = await run_factory(session.id, user.id)
    service = ArtifactService(db_session)

    artifact, draft, _ = await _draft_brand(db_session, user, session, run, service)
    await service.advance_read_state(user.id, session.id, "brand", 1)

    await service.update_draft(
        run_id=run.id, draft_id=draft.id, payload={"data": {"overview": {"x": 2}}},
        evidence_refs=[],
    )
    # 前端渲染到 sequence=2
    assert await service.advance_read_state(user.id, session.id, "brand", 2) == 2

    # 更小的 stale 写入不后退
    assert await service.advance_read_state(user.id, session.id, "brand", 1) == 2
    assert await service.get_unread(user.id, session.id, "brand") is False

    # 一次性推进到更远水位，之后的低水位写也不后退
    assert await service.advance_read_state(user.id, session.id, "brand", 5) == 5
    assert await service.advance_read_state(user.id, session.id, "brand", 2) == 5


async def test_unread_is_per_module(
    db_session, user_factory, session_factory, run_factory, legacy_session_factory,
):
    user = await user_factory()
    session = await session_factory(user.id)
    await legacy_session_factory(user.id, session.id)
    run = await run_factory(session.id, user.id)
    service = ArtifactService(db_session)

    await _draft_brand(db_session, user, session, run, service)  # brand seq 1
    await service.advance_read_state(user.id, session.id, "brand", 1)
    assert await service.get_unread(user.id, session.id, "brand") is False
    assert await service.get_unread(user.id, session.id, "campaign") is False

    await service.create_or_get_draft(
        session_id=session.id,
        user_id=user.id,
        run_id=run.id,
        module="campaign",
        business_fields={"brand": "瑞幸", "campaign": "双十一"},
        schema_version="campaign_report_v2",
        payload={"data": {}},
        evidence_refs=[],
        artifact_type="campaign_report_v2",
    )  # campaign seq 2

    assert await service.get_unread(user.id, session.id, "campaign") is True
    assert await service.get_unread(user.id, session.id, "brand") is False


async def test_advance_read_state_merges_legacy_module_key_row(
    db_session, user_factory, session_factory, legacy_session_factory,
):
    user = await user_factory()
    session = await session_factory(user.id)
    await legacy_session_factory(user.id, session.id)
    service = ArtifactService(db_session)

    # 遗留写入方（app/artifacts/service）留下的行：module_key 已设置，module/last_seen_sequence 为 NULL
    legacy_state = ArtifactReadState(
        id=str(uuid4()),
        user_id=user.id,
        session_id=session.id,
        module_key="brand",
        last_seen_artifact_id=None,
        seen_at=datetime.now(UTC).replace(tzinfo=None),
    )
    db_session.add(legacy_state)
    await db_session.flush()

    # 新写入按新列 module 查不到，但必须 UPSERT 进遗留行，而不是撞遗留唯一约束
    assert await service.advance_read_state(user.id, session.id, "brand", 5) == 5

    state = await db_session.scalar(
        select(AgentArtifactReadState).where(
            AgentArtifactReadState.user_id == user.id,
            AgentArtifactReadState.session_id == session.id,
            AgentArtifactReadState.module == "brand",
        )
    )
    assert state is not None
    assert state.module == "brand"
    assert state.last_seen_sequence == 5

    # 只更新遗留行，不新增行
    count = await db_session.scalar(
        select(func.count())
        .select_from(AgentArtifactReadState.__table__)
        .where(
            AgentArtifactReadState.user_id == user.id,
            AgentArtifactReadState.session_id == session.id,
        )
    )
    assert count == 1

    # 水位继续前进：再次写入更新同一行
    assert await service.advance_read_state(user.id, session.id, "brand", 2) == 5
    count = await db_session.scalar(
        select(func.count())
        .select_from(AgentArtifactReadState.__table__)
        .where(
            AgentArtifactReadState.user_id == user.id,
            AgentArtifactReadState.session_id == session.id,
        )
    )
    assert count == 1
