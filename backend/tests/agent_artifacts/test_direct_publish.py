"""确定性直接发布服务测试（直接发布改造 Task 3；设计 §10.2/§10.3）。

覆盖：
1. 逐 Artifact 独立发布：同一调用中合法 Draft 发布成功、非法 Draft
   ``validation_failed``，一个失败不回滚其他成功项；
2. 同一 Draft Revision 幂等（``idempotency_key``）：重放返回同一 Version，
   不产生重复 Version / Attempt；
3. 单次调用内重复 draft id 去重：同一 Draft 不会被发布两次；
4. 校验快照写入 ``ArtifactPublishAttempt.validation_json`` 与 Version
   ``validation_json``；新 Version ``review_json=None``，不写任何 Review 表；
5. 发布成功释放 working head 并追加 ``published`` 事件；``validation_failed``
   保留 owner（Draft 仍 drafting）供模型修订后重发，重放不重复记 Attempt；
6. 租约校验：调用方 worker 不持有 Run 活跃租约时抛 ``run_lease_not_held``；
7. 归属护栏：Draft 不存在 / 被其他 Run 持有 → 逐项 ``failed``，不阻断其他项。
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_artifacts.models import (
    AgentArtifact,
    AgentArtifactVersion,
    ArtifactDraft,
    ArtifactDraftRevision,
    ArtifactEvent,
    ArtifactPublishAttempt,
    ArtifactReviewAttempt,
    ArtifactReviewBatch,
    ArtifactReviewItem,
)
from app.agent_artifacts.publishing import ArtifactPublicationService
from app.agent_artifacts.service import ArtifactService
from app.agent_runtime.models import AgentRun, AgentSession
from app.agent_runtime.repository import AgentRunRepository
from app.agent_runtime.state import InvalidRunTransition
from app.identity.models import User

from tests.agent_artifacts.payload_fixtures import insight_metric_payload, insight_payload

WORKER = "worker-1"


@dataclass(frozen=True)
class DraftHandle:
    """测试用 Draft 句柄：发布服务入参/断言所需的标识集合。"""

    id: str  # draft id
    run_id: str
    artifact_id: str
    revision_id: str


@dataclass(frozen=True)
class PublishEnv:
    db: AsyncSession
    user: User
    session: AgentSession
    run: AgentRun


async def _make_draft(
    env: PublishEnv,
    *,
    question: str,
    payload: dict | None = None,
    evidence_refs: list[dict] | None = None,
) -> DraftHandle:
    artifact, draft, revision = await ArtifactService(env.db).create_or_get_draft(
        session_id=env.session.id,
        user_id=env.user.id,
        run_id=env.run.id,
        module="insight",
        business_fields={"parent_artifact_version_id": f"pv-{question}", "question": question},
        schema_version="insight_board_v1",
        artifact_type="insight_board_v1",
        payload=payload if payload is not None else insight_payload(title=question),
        evidence_refs=evidence_refs or [],
    )
    return DraftHandle(
        id=draft.id,
        run_id=env.run.id,
        artifact_id=artifact.id,
        revision_id=revision.id,
    )


@pytest_asyncio.fixture
async def publish_env(db_session, user_factory, session_factory, run_factory) -> PublishEnv:
    """用户 + 会话 + 运行中 Run，且 WORKER 已持有该 Run 的活跃租约。"""
    user = await user_factory()
    session = await session_factory(user.id)
    run = await run_factory(session.id, user.id)
    claimed = await AgentRunRepository(db_session).claim_lease(run.id, WORKER, 300)
    assert claimed
    return PublishEnv(db=db_session, user=user, session=session, run=run)


@pytest_asyncio.fixture
async def publication_service(db_session) -> ArtifactPublicationService:
    return ArtifactPublicationService(db_session)


@pytest_asyncio.fixture
async def valid_draft(publish_env) -> DraftHandle:
    return await _make_draft(publish_env, question="合法")


@pytest_asyncio.fixture
async def invalid_draft(publish_env) -> DraftHandle:
    """落库后 payload 被旁路污染的 Draft：发布边界二次校验必须拦下。"""
    handle = await _make_draft(publish_env, question="非法")
    revision = await publish_env.db.get(ArtifactDraftRevision, handle.revision_id)
    assert revision is not None
    revision.payload_json = {"data": {"overview": {"total_volume": 100}}}
    await publish_env.db.flush()
    return handle


async def _attempts_for(db: AsyncSession, revision_id: str) -> list[ArtifactPublishAttempt]:
    return list(
        (
            await db.scalars(
                select(ArtifactPublishAttempt).where(
                    ArtifactPublishAttempt.draft_revision_id == revision_id
                )
            )
        ).all()
    )


# ---------------------------------------------------------------------------
# 1. 逐 Artifact 发布 + 幂等（brief 契约测试）
# ---------------------------------------------------------------------------


async def test_publish_is_per_artifact_and_idempotent(
    publication_service, valid_draft, invalid_draft
):
    results = await publication_service.publish(
        run_id=valid_draft.run_id,
        draft_ids=(valid_draft.id, invalid_draft.id),
        worker_id="worker-1",
    )
    assert [item.status for item in results] == ["published", "validation_failed"]
    replay = await publication_service.publish(
        run_id=valid_draft.run_id,
        draft_ids=(valid_draft.id,),
        worker_id="worker-1",
    )
    assert replay[0].artifact_version_id == results[0].artifact_version_id


async def test_publish_replay_creates_no_duplicate_version_or_attempt(
    publication_service, publish_env, valid_draft
):
    first = await publication_service.publish(
        run_id=valid_draft.run_id, draft_ids=(valid_draft.id,), worker_id=WORKER
    )
    replay = await publication_service.publish(
        run_id=valid_draft.run_id, draft_ids=(valid_draft.id,), worker_id=WORKER
    )
    assert replay[0].status == "published"
    assert replay[0].artifact_version_id == first[0].artifact_version_id
    assert replay[0].version == first[0].version

    version_count = await publish_env.db.scalar(
        select(func.count(AgentArtifactVersion.id)).where(
            AgentArtifactVersion.artifact_id == valid_draft.artifact_id
        )
    )
    assert version_count == 1
    assert len(await _attempts_for(publish_env.db, valid_draft.revision_id)) == 1


async def test_publish_deduplicates_draft_ids_within_single_call(
    publication_service, publish_env, valid_draft
):
    """PublishArtifacts schema 不拒绝重复 id：服务端必须幂等去重，同调不双发。"""
    results = await publication_service.publish(
        run_id=valid_draft.run_id,
        draft_ids=(valid_draft.id, valid_draft.id),
        worker_id=WORKER,
    )
    assert [item.status for item in results] == ["published"]
    version_count = await publish_env.db.scalar(
        select(func.count(AgentArtifactVersion.id)).where(
            AgentArtifactVersion.artifact_id == valid_draft.artifact_id
        )
    )
    assert version_count == 1
    assert len(await _attempts_for(publish_env.db, valid_draft.revision_id)) == 1


# ---------------------------------------------------------------------------
# 2. 校验快照 / Version 形态 / Review 表零写入
# ---------------------------------------------------------------------------


async def test_publish_writes_validation_snapshot_and_null_review(
    publication_service, publish_env, valid_draft
):
    results = await publication_service.publish(
        run_id=valid_draft.run_id, draft_ids=(valid_draft.id,), worker_id=WORKER
    )
    item = results[0]
    assert item.status == "published"
    assert item.artifact_id == valid_draft.artifact_id
    assert item.artifact_version_id is not None
    assert item.version == 1
    assert item.errors == ()

    attempts = await _attempts_for(publish_env.db, valid_draft.revision_id)
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt.status == "published"
    assert attempt.run_id == valid_draft.run_id
    assert attempt.artifact_id == valid_draft.artifact_id
    assert attempt.published_version_id == item.artifact_version_id
    assert attempt.completed_at is not None
    snapshot = attempt.validation_json
    assert snapshot["valid"] is True
    assert snapshot["errors"] == []
    assert snapshot["stages"]["payload"] == {"valid": True, "errors": []}
    assert snapshot["stages"]["lineage"] == {"valid": True, "errors": []}

    version = await publish_env.db.get(AgentArtifactVersion, item.artifact_version_id)
    assert version is not None
    assert version.review_json is None
    assert version.validation_json == snapshot
    assert version.data_status == "complete"
    assert version.lineage_snapshot_json == {"refs": []}

    artifact = await publish_env.db.get(AgentArtifact, valid_draft.artifact_id)
    assert artifact is not None
    assert artifact.latest_version == 1
    assert artifact.status == "published"

    draft = await publish_env.db.get(ArtifactDraft, valid_draft.id)
    assert draft is not None
    assert draft.status == "idle"
    assert draft.owner_run_id is None

    published_event = await publish_env.db.scalar(
        select(ArtifactEvent).where(
            ArtifactEvent.artifact_id == valid_draft.artifact_id,
            ArtifactEvent.event_type == "published",
        )
    )
    assert published_event is not None
    assert published_event.artifact_version_id == item.artifact_version_id
    assert published_event.draft_revision == 1


async def test_publish_never_writes_review_tables(
    publication_service, publish_env, valid_draft
):
    await publication_service.publish(
        run_id=valid_draft.run_id, draft_ids=(valid_draft.id,), worker_id=WORKER
    )
    for model in (ArtifactReviewBatch, ArtifactReviewItem, ArtifactReviewAttempt):
        assert (await publish_env.db.scalar(select(func.count(model.id)))) == 0


# ---------------------------------------------------------------------------
# 3. 校验失败：结构化快照 + 保留 owner 供修订重发
# ---------------------------------------------------------------------------


async def test_validation_failure_keeps_draft_owned_and_records_attempt(
    publication_service, publish_env, invalid_draft
):
    results = await publication_service.publish(
        run_id=invalid_draft.run_id, draft_ids=(invalid_draft.id,), worker_id=WORKER
    )
    item = results[0]
    assert item.status == "validation_failed"
    assert item.artifact_version_id is None
    assert item.version is None
    assert item.errors

    attempts = await _attempts_for(publish_env.db, invalid_draft.revision_id)
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt.status == "validation_failed"
    assert attempt.error_code == "artifact_payload_invalid"
    assert attempt.published_version_id is None
    snapshot = attempt.validation_json
    assert snapshot["valid"] is False
    assert snapshot["stages"]["payload"]["valid"] is False
    assert snapshot["errors"]

    # 不发 Version、不发 published 事件；Draft 保留 owner 供模型修订后重发。
    version_count = await publish_env.db.scalar(
        select(func.count(AgentArtifactVersion.id)).where(
            AgentArtifactVersion.artifact_id == invalid_draft.artifact_id
        )
    )
    assert version_count == 0
    draft = await publish_env.db.get(ArtifactDraft, invalid_draft.id)
    assert draft is not None
    assert draft.status == "drafting"
    assert draft.owner_run_id == invalid_draft.run_id

    # 同一 Revision 重放校验失败幂等：不重复记 Attempt。
    replay = await publication_service.publish(
        run_id=invalid_draft.run_id, draft_ids=(invalid_draft.id,), worker_id=WORKER
    )
    assert replay[0].status == "validation_failed"
    assert replay[0].errors == item.errors
    assert len(await _attempts_for(publish_env.db, invalid_draft.revision_id)) == 1


async def test_lineage_gap_is_validation_failed(
    publication_service, publish_env
):
    """payload 合法但业务数字缺 lineage：发布门禁判 validation_failed。"""
    handle = await _make_draft(
        publish_env, question="缺 lineage", payload=insight_metric_payload(value=100)
    )
    results = await publication_service.publish(
        run_id=handle.run_id, draft_ids=(handle.id,), worker_id=WORKER
    )
    item = results[0]
    assert item.status == "validation_failed"
    assert any(error.get("code") == "missing_lineage" for error in item.errors)
    assert any(error.get("stage") == "lineage" for error in item.errors)

    attempts = await _attempts_for(publish_env.db, handle.revision_id)
    assert len(attempts) == 1
    assert attempts[0].status == "validation_failed"
    assert attempts[0].error_code == "missing_lineage"
    snapshot = attempts[0].validation_json
    assert snapshot["stages"]["payload"]["valid"] is True
    assert snapshot["stages"]["lineage"]["valid"] is False


# ---------------------------------------------------------------------------
# 4. 租约与归属护栏
# ---------------------------------------------------------------------------


async def test_publish_requires_run_lease(
    publication_service, db_session, user_factory, session_factory, run_factory
):
    """未持有租约 / 他人持有租约：抛 run_lease_not_held，不发布任何项。"""
    user = await user_factory()
    session = await session_factory(user.id)
    run = await run_factory(session.id, user.id)
    env = PublishEnv(db=db_session, user=user, session=session, run=run)
    handle = await _make_draft(env, question="无租约")

    with pytest.raises(InvalidRunTransition):
        await publication_service.publish(
            run_id=handle.run_id, draft_ids=(handle.id,), worker_id=WORKER
        )

    claimed = await AgentRunRepository(db_session).claim_lease(run.id, "other-worker", 300)
    assert claimed
    with pytest.raises(InvalidRunTransition):
        await publication_service.publish(
            run_id=handle.run_id, draft_ids=(handle.id,), worker_id=WORKER
        )


async def test_publish_foreign_owned_draft_fails_without_blocking_others(
    publication_service, publish_env, run_factory, valid_draft
):
    """他人持有的 Draft 逐项 failed（artifact_busy），同一调用中自己的 Draft 照常发布。"""
    other_run = await run_factory(publish_env.session.id, publish_env.user.id)
    other_env = PublishEnv(
        db=publish_env.db, user=publish_env.user, session=publish_env.session, run=other_run
    )
    foreign = await _make_draft(other_env, question="他人 Draft")

    results = await publication_service.publish(
        run_id=valid_draft.run_id,
        draft_ids=(foreign.id, valid_draft.id),
        worker_id=WORKER,
    )
    assert [item.status for item in results] == ["failed", "published"]
    assert results[0].draft_id == foreign.id
    assert any(error.get("code") == "artifact_busy" for error in results[0].errors)
    # 归属失败不写 PublishAttempt（未进入校验），他人 Draft 不受影响。
    assert await _attempts_for(publish_env.db, foreign.revision_id) == []
    foreign_draft = await publish_env.db.get(ArtifactDraft, foreign.id)
    assert foreign_draft is not None
    assert foreign_draft.owner_run_id == other_run.id


async def test_publish_unknown_draft_is_per_item_failure(
    publication_service, valid_draft
):
    results = await publication_service.publish(
        run_id=valid_draft.run_id,
        draft_ids=("missing-draft-id", valid_draft.id),
        worker_id=WORKER,
    )
    assert [item.status for item in results] == ["failed", "published"]
    assert results[0].draft_id == "missing-draft-id"
    assert results[0].artifact_version_id is None
    assert any(error.get("code") == "draft_not_found" for error in results[0].errors)
