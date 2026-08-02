"""Artifact Draft / Revision 生命周期测试（设计文档 §8.1 / Task 12）。

覆盖：
1. 新 Draft 先建稳定 agent_artifacts 身份，再建 working head 与首个 Revision；
2. 更新插入不可变新 Revision，current_revision 前进，旧 Revision 保留；
3. artifact_busy：两个活动 Run 同时更新同一 Artifact，第二个得到结构化 busy，
   不崩溃、不静默覆盖；owner 释放后新 Run 才能接管；
4. parent_artifact_version_id 只写在 Draft Revision 与 Published Version，
   稳定 agent_artifacts 行没有该字段；
5. Draft 更新递增 Session 级 artifact sequence，事件携带 draft_revision + artifact_id；
6. 发布（release）把 working head 置回 idle、释放 owner_run_id，历史 Revision 保留。
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.agent_artifacts.models import (
    AgentArtifact,
    AgentArtifactVersion,
    ArtifactDraft,
    ArtifactDraftRevision,
    ArtifactEvent,
)
from app.agent_artifacts.service import ArtifactBusy, ArtifactService

BRAND_PAYLOAD_V1 = {"data": {"overview": {"total_volume": 100}}}
BRAND_PAYLOAD_V2 = {"data": {"overview": {"total_volume": 150}}}
EVIDENCE_V2 = [
    {"artifact_path": "/data/overview/total_volume", "sources": [{"source_type": "evidence", "evidence_id": "e-1", "source_path": "/0/声量"}]}
]


async def test_create_draft_creates_identity_head_and_first_revision(
    db_session, user_factory, session_factory, run_factory,
):
    user = await user_factory()
    session = await session_factory(user.id)
    run = await run_factory(session.id, user.id)
    service = ArtifactService(db_session)

    artifact, draft, revision = await service.create_or_get_draft(
        session_id=session.id,
        user_id=user.id,
        run_id=run.id,
        module="brand",
        business_fields={"brand": "瑞幸 Coffee"},
        schema_version="brand_report_v3",
        payload=BRAND_PAYLOAD_V1,
        evidence_refs=[],
        artifact_type="brand_report_v3",
    )

    assert artifact.artifact_key == "brand:瑞幸 coffee"
    assert artifact.status == "draft"
    assert artifact.latest_version == 0
    assert artifact.activity_sequence == 1

    assert draft.artifact_id == artifact.id
    assert draft.owner_run_id == run.id
    assert draft.status == "drafting"
    assert draft.current_revision == 1
    assert draft.revision_count == 1

    assert revision.revision == 1
    assert revision.run_id == run.id
    assert revision.payload_json == BRAND_PAYLOAD_V1

    # 稳定身份 / working head / Revision 各一条
    assert len((await db_session.scalars(select(AgentArtifact))).all()) == 1
    assert len((await db_session.scalars(select(ArtifactDraft))).all()) == 1
    assert len((await db_session.scalars(select(ArtifactDraftRevision))).all()) == 1


async def test_update_draft_appends_immutable_revision_and_advances_current(
    db_session, user_factory, session_factory, run_factory,
):
    user = await user_factory()
    session = await session_factory(user.id)
    run = await run_factory(session.id, user.id)
    service = ArtifactService(db_session)

    artifact, draft, _ = await service.create_or_get_draft(
        session_id=session.id,
        user_id=user.id,
        run_id=run.id,
        module="brand",
        business_fields={"brand": "瑞幸"},
        schema_version="brand_report_v3",
        payload=BRAND_PAYLOAD_V1,
        evidence_refs=[],
        artifact_type="brand_report_v3",
    )

    draft2, revision2 = await service.update_draft(
        run_id=run.id,
        draft_id=draft.id,
        payload=BRAND_PAYLOAD_V2,
        evidence_refs=EVIDENCE_V2,
    )
    assert revision2.revision == 2
    assert draft2.current_revision == 2
    assert draft2.revision_count == 2

    # 旧 Revision 保留（不可变，内容不回写）
    revisions = (
        await db_session.scalars(
            select(ArtifactDraftRevision)
            .where(ArtifactDraftRevision.draft_id == draft.id)
            .order_by(ArtifactDraftRevision.revision)
        )
    ).all()
    assert [r.revision for r in revisions] == [1, 2]
    assert revisions[0].payload_json == BRAND_PAYLOAD_V1
    assert revisions[1].payload_json == BRAND_PAYLOAD_V2

    # 事件：Session 级 sequence 递增，携带 draft_revision 与稳定 artifact_id
    events = (
        await db_session.scalars(
            select(ArtifactEvent)
            .where(ArtifactEvent.session_id == session.id)
            .order_by(ArtifactEvent.sequence)
        )
    ).all()
    assert [(e.event_type, e.sequence) for e in events] == [
        ("draft_created", 1),
        ("draft_updated", 2),
    ]
    assert events[1].artifact_id == artifact.id
    assert events[1].draft_revision == 2


async def test_second_active_run_gets_artifact_busy(
    db_session, user_factory, session_factory, run_factory,
):
    user = await user_factory()
    session = await session_factory(user.id)
    run_a = await run_factory(session.id, user.id)
    run_b = await run_factory(session.id, user.id)
    service = ArtifactService(db_session)

    business_fields = {"brand": "瑞幸"}
    artifact, draft, _ = await service.create_or_get_draft(
        session_id=session.id,
        user_id=user.id,
        run_id=run_a.id,
        module="brand",
        business_fields=business_fields,
        schema_version="brand_report_v3",
        payload=BRAND_PAYLOAD_V1,
        evidence_refs=[],
        artifact_type="brand_report_v3",
    )

    # 第二个活动 Run 抢同一 Artifact → 结构化 artifact_busy，不崩溃
    with pytest.raises(ArtifactBusy) as excinfo:
        await service.create_or_get_draft(
            session_id=session.id,
            user_id=user.id,
            run_id=run_b.id,
            module="brand",
            business_fields=business_fields,
            schema_version="brand_report_v3",
            payload=BRAND_PAYLOAD_V2,
            evidence_refs=EVIDENCE_V2,
            artifact_type="brand_report_v3",
        )
    assert excinfo.value.code == "artifact_busy"

    # 不静默覆盖：owner 仍是 run_a，revision 未前进
    assert draft.owner_run_id == run_a.id
    assert draft.current_revision == 1
    assert len((await db_session.scalars(select(ArtifactDraftRevision))).all()) == 1

    # 非 owner 的 update 同样 busy
    with pytest.raises(ArtifactBusy):
        await service.update_draft(
            run_id=run_b.id,
            draft_id=draft.id,
            payload=BRAND_PAYLOAD_V2,
            evidence_refs=EVIDENCE_V2,
        )


async def test_owner_switches_only_after_previous_owner_releases(
    db_session, user_factory, session_factory, run_factory,
):
    user = await user_factory()
    session = await session_factory(user.id)
    run_a = await run_factory(session.id, user.id)
    run_b = await run_factory(session.id, user.id)
    service = ArtifactService(db_session)

    business_fields = {"brand": "瑞幸"}
    artifact, draft, _ = await service.create_or_get_draft(
        session_id=session.id,
        user_id=user.id,
        run_id=run_a.id,
        module="brand",
        business_fields=business_fields,
        schema_version="brand_report_v3",
        payload=BRAND_PAYLOAD_V1,
        evidence_refs=[],
        artifact_type="brand_report_v3",
    )

    await service.mark_draft_reviewing(run_a.id, draft.id)
    assert draft.status == "reviewing"
    await service.release_draft(draft.id, outcome="idle")

    assert draft.status == "idle"
    assert draft.owner_run_id is None

    # 新 Run 接管同一 working head，在历史 Revision 上继续递增
    artifact_b, draft_b, revision_b = await service.create_or_get_draft(
        session_id=session.id,
        user_id=user.id,
        run_id=run_b.id,
        module="brand",
        business_fields=business_fields,
        schema_version="brand_report_v3",
        payload=BRAND_PAYLOAD_V2,
        evidence_refs=EVIDENCE_V2,
        artifact_type="brand_report_v3",
    )
    assert artifact_b.id == artifact.id
    assert draft_b.id == draft.id
    assert draft_b.owner_run_id == run_b.id
    assert draft_b.status == "drafting"
    assert revision_b.revision == 2
    assert draft_b.current_revision == 2


async def test_parent_artifact_version_id_lives_on_revision_not_stable_row(
    db_session, user_factory, session_factory, run_factory,
):
    user = await user_factory()
    session = await session_factory(user.id)
    run = await run_factory(session.id, user.id)
    service = ArtifactService(db_session)

    # 先发布一个父 Artifact 版本（最小手动发布，Task 13 之前）
    parent_artifact, parent_draft, parent_rev = await service.create_or_get_draft(
        session_id=session.id,
        user_id=user.id,
        run_id=run.id,
        module="brand",
        business_fields={"brand": "瑞幸"},
        schema_version="brand_report_v3",
        payload=BRAND_PAYLOAD_V1,
        evidence_refs=[],
        artifact_type="brand_report_v3",
    )
    parent_version = AgentArtifactVersion(
        artifact_id=parent_artifact.id,
        version=1,
        source_run_id=run.id,
        source_draft_revision_id=parent_rev.id,
        schema_version="brand_report_v3",
        payload_json=BRAND_PAYLOAD_V1,
        evidence_refs_json=[],
        review_json=None,
        data_status="complete",
        created_at=datetime.now(UTC).replace(tzinfo=None),
    )
    db_session.add(parent_version)
    await db_session.flush()

    child_artifact, child_draft, child_rev = await service.create_or_get_draft(
        session_id=session.id,
        user_id=user.id,
        run_id=run.id,
        module="insight",
        business_fields={
            "parent_artifact_version_id": parent_version.id,
            "question": "为什么下降",
        },
        schema_version="insight_board_v1",
        payload={"data": {"metrics": []}},
        evidence_refs=[],
        artifact_type="insight_board_v1",
        parent_artifact_id=parent_artifact.id,
        parent_artifact_version_id=parent_version.id,
    )

    # parent_artifact_version_id 只写在 Draft Revision（与发布 Version）
    assert child_rev.parent_artifact_version_id == parent_version.id
    # 稳定身份行没有该列，也没有该属性
    assert "parent_artifact_version_id" not in [
        col.name for col in AgentArtifact.__table__.columns
    ]
    assert not hasattr(child_artifact, "parent_artifact_version_id")


async def test_release_preserves_historical_revisions(
    db_session, user_factory, session_factory, run_factory,
):
    user = await user_factory()
    session = await session_factory(user.id)
    run = await run_factory(session.id, user.id)
    service = ArtifactService(db_session)

    artifact, draft, _ = await service.create_or_get_draft(
        session_id=session.id,
        user_id=user.id,
        run_id=run.id,
        module="brand",
        business_fields={"brand": "瑞幸"},
        schema_version="brand_report_v3",
        payload=BRAND_PAYLOAD_V1,
        evidence_refs=[],
        artifact_type="brand_report_v3",
    )
    await service.update_draft(
        run_id=run.id, draft_id=draft.id, payload=BRAND_PAYLOAD_V2, evidence_refs=EVIDENCE_V2,
    )
    await service.mark_draft_reviewing(run.id, draft.id)
    await service.release_draft(draft.id, outcome="idle")

    assert draft.status == "idle"
    assert draft.owner_run_id is None
    assert draft.current_revision == 2
    revisions = (
        await db_session.scalars(
            select(ArtifactDraftRevision)
            .where(ArtifactDraftRevision.draft_id == draft.id)
            .order_by(ArtifactDraftRevision.revision)
        )
    ).all()
    assert [r.revision for r in revisions] == [1, 2]
