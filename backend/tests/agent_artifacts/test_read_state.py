"""未读水位（agent_artifact_read_states）测试（设计文档 §8.1 ReadState / Task 12）。

覆盖：
1. 模块最新 artifact_events.sequence > last_seen_sequence 时未读；标记已读后消除；
2. last_seen_sequence 只前进到前端已渲染的 sequence（max(old, new)）；
3. 更小的（stale）sequence 写入不会让水位倒退；
4. 未读按 (user, session, module) 隔离；
5. 水位写入独立的 agent_artifact_read_states，不再触碰遗留 artifact_read_states。
"""

from sqlalchemy import func, select

from app.agent_artifacts.models import AgentArtifactReadState
from app.agent_artifacts.service import ArtifactService
from app.artifacts.models import ArtifactReadState

from tests.agent_artifacts.payload_fixtures import brand_payload, campaign_payload


async def _draft_brand(db_session, user, session, run, service, brand="瑞幸"):
    artifact, draft, revision = await service.create_or_get_draft(
        session_id=session.id,
        user_id=user.id,
        run_id=run.id,
        module="brand",
        business_fields={"brand": brand},
        schema_version="brand_report_v3",
        payload=brand_payload(),
        evidence_refs=[],
        artifact_type="brand_report_v3",
    )
    return artifact, draft, revision


async def test_unread_until_marked_seen(
    db_session, user_factory, session_factory, run_factory,
):
    user = await user_factory()
    session = await session_factory(user.id)
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
        run_id=run.id, draft_id=draft.id, payload=brand_payload(),
        evidence_refs=[],
    )
    assert await service.get_unread(user.id, session.id, "brand") is True
    await service.advance_read_state(user.id, session.id, "brand", 2)
    assert await service.get_unread(user.id, session.id, "brand") is False


async def test_read_state_watermark_only_advances_and_never_backwards(
    db_session, user_factory, session_factory, run_factory,
):
    user = await user_factory()
    session = await session_factory(user.id)
    run = await run_factory(session.id, user.id)
    service = ArtifactService(db_session)

    artifact, draft, _ = await _draft_brand(db_session, user, session, run, service)
    await service.advance_read_state(user.id, session.id, "brand", 1)

    await service.update_draft(
        run_id=run.id, draft_id=draft.id, payload=brand_payload(),
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
    db_session, user_factory, session_factory, run_factory,
):
    user = await user_factory()
    session = await session_factory(user.id)
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
        payload=campaign_payload(),
        evidence_refs=[],
        artifact_type="campaign_report_v2",
    )  # campaign seq 2

    assert await service.get_unread(user.id, session.id, "campaign") is True
    assert await service.get_unread(user.id, session.id, "brand") is False


async def test_advance_read_state_writes_new_table_and_leaves_legacy_untouched(
    db_session, user_factory, session_factory,
):
    """迁移 0028 后：水位只写 agent_artifact_read_states（无需遗留 sessions 行），
    遗留 artifact_read_states 表保持不动。"""
    user = await user_factory()
    session = await session_factory(user.id)
    service = ArtifactService(db_session)

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

    # 单调推进仍更新同一行，不新增行
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

    # 遗留表不再被新代码路径写入
    legacy_count = await db_session.scalar(
        select(func.count()).select_from(ArtifactReadState.__table__)
    )
    assert legacy_count == 0
