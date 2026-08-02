"""多 Artifact 批量原子发布测试（设计 §12.3 / §八 / Task 13）。

覆盖：
1. 整批全部 approve → 在同一事务内插入全部 Version + 写 assistant 消息 +
   Draft working head 置回 idle 并释放 owner；
2. 任一 reject → 不产生任何 Version（all-or-nothing），Run failed；
3. 未修改且已 approve 的 Revision 在下一轮复核中复用；被修改的 approve
   自动失效、必须重新审核；
4. batch/run 级 review_count + revision_count 汇总正确。
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import func, select

from app.agent_artifacts.models import (
    AgentArtifact,
    AgentArtifactVersion,
    ArtifactReviewAttempt,
    ArtifactReviewItem,
)
from app.agent_artifacts.service import ArtifactService
from app.agent_runtime.model_gateway import AgentModelGateway
from app.agent_runtime.models import AgentMessage, AgentRun
from app.agent_runtime.repository import AgentRunRepository
from app.agent_runtime.reviewer import ReviewerDriver
from app.agent_runtime.state import RunStatus
from app.model.prompt_logs import PromptLogEntry
from app.model.tencent_plan import TencentPlanAdapter

APPROVE_JSON = '{"decision":"approve","issues":[]}'
REVISE_JSON = (
    '{"decision":"revise","issues":[{"code":"missing_data","message":"需要补查"}]}'
)
REJECT_JSON = '{"decision":"reject","issues":[{"code":"untrusted","message":"无法追溯"}]}'

# 无必需数字叶子，lineage 解析为空闭包，测试无需建 Evidence。
PAYLOAD_V1 = {"data": {"overview": {"brand": "瑞幸"}}}
PAYLOAD_V2 = {"data": {"overview": {"brand": "瑞幸", "note": "补查后"}}}


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def stream_chunks(*, content_chunks: list[str | None], reasoning_chunks: list[str | None]) -> Any:
    chunks = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content=content, reasoning_content=reasoning),
                    finish_reason=None,
                )
            ],
            usage=None,
            _request_id="req-review",
        )
        for content, reasoning in zip(content_chunks, reasoning_chunks, strict=True)
    ]
    chunks.append(
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content=None, reasoning_content=None),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=3,
                completion_tokens=2,
                total_tokens=5,
                prompt_tokens_details=None,
                completion_tokens_details=None,
            ),
            _request_id="req-review",
        )
    )

    async def stream() -> Any:
        for chunk in chunks:
            yield chunk

    return stream()


class FakeCompletions:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.outcomes.pop(0)


class _CaptureWriter:
    def __init__(self) -> None:
        self.entries: list[PromptLogEntry] = []

    async def __call__(self, entry: PromptLogEntry) -> None:
        self.entries.append(entry)


def _make_gateway(db_session, decision_jsons: list[str]) -> AgentModelGateway:
    client = FakeCompletions(
        [stream_chunks(content_chunks=[j], reasoning_chunks=[None]) for j in decision_jsons]
    )
    adapter = TencentPlanAdapter(
        client=client,
        log_writer=_CaptureWriter(),
        stream_support_cache={},
    )
    return AgentModelGateway(adapter, db=db_session)


async def _make_draft(
    service: ArtifactService,
    *,
    session_id: str,
    user_id: str,
    run_id: str,
    brand: str,
    payload: dict[str, Any],
):
    return await service.create_or_get_draft(
        session_id=session_id,
        user_id=user_id,
        run_id=run_id,
        module="brand",
        business_fields={"brand": brand},
        schema_version="brand_report_v3",
        payload=payload,
        evidence_refs=[],
        artifact_type="brand_report_v3",
    )


async def _submit(run: AgentRun, db_session, worker: str = "worker") -> AgentRunRepository:
    repo = AgentRunRepository(db_session)
    await repo.claim_lease(run.id, worker, 300)
    await repo.transition(run.id, RunStatus.REVIEWING, worker_id=worker)
    return repo


async def _items_for(db_session, batch_id: str) -> list[ArtifactReviewItem]:
    return (
        await db_session.scalars(
            select(ArtifactReviewItem)
            .where(ArtifactReviewItem.batch_id == batch_id)
            .order_by(ArtifactReviewItem.artifact_id)
        )
    ).all()


async def test_batch_publishes_all_versions_atomically_when_all_approved(
    db_session, user_factory, session_factory, run_factory
) -> None:
    user = await user_factory()
    session = await session_factory(user.id)
    run = await run_factory(session.id, user.id)
    service = ArtifactService(db_session)
    await _submit(run, db_session)

    artifact_a, draft_a, _ = await _make_draft(
        service, session_id=session.id, user_id=user.id, run_id=run.id,
        brand="瑞幸", payload=PAYLOAD_V1,
    )
    artifact_b, draft_b, _ = await _make_draft(
        service, session_id=session.id, user_id=user.id, run_id=run.id,
        brand="库迪", payload=PAYLOAD_V1,
    )

    gateway = _make_gateway(db_session, [APPROVE_JSON, APPROVE_JSON])
    driver = ReviewerDriver(db_session, gateway, worker_id="worker")
    batch = await driver.create_batch(
        parent_run_id=run.id, draft_ids=(draft_a.id, draft_b.id), completion_text="完成分析"
    )
    items = await _items_for(db_session, batch.id)
    assert len(items) == 2

    # 依次审两个 Item（与网关 outcome 顺序一致）
    for item in items:
        result = await driver.review_item(parent_run=run, item=item, user_question="分析")
        assert result.decision == "approve"

    versions = await service.publish_batch(batch.id, worker_id="worker")
    assert len(versions) == 2
    assert {v.artifact_id for v in versions} == {artifact_a.id, artifact_b.id}
    assert {v.version for v in versions} == {1, 1}

    # 稳定身份 latest_version/status 更新
    artifact_a_row = await db_session.get(AgentArtifact, artifact_a.id)
    artifact_b_row = await db_session.get(AgentArtifact, artifact_b.id)
    assert artifact_a_row is not None and artifact_a_row.latest_version == 1
    assert artifact_a_row.status == "published"
    assert artifact_b_row is not None and artifact_b_row.latest_version == 1
    assert artifact_b_row.status == "published"

    # working head 置回 idle 并释放 owner
    draft_a_row = await db_session.get(type(draft_a), draft_a.id)
    draft_b_row = await db_session.get(type(draft_b), draft_b.id)
    assert draft_a_row is not None and draft_a_row.status == "idle"
    assert draft_a_row.owner_run_id is None
    assert draft_b_row is not None and draft_b_row.status == "idle"
    assert draft_b_row.owner_run_id is None

    # assistant 消息写入（completion_text）
    msg = await db_session.scalar(
        select(AgentMessage).where(AgentMessage.session_id == session.id)
    )
    assert msg is not None
    assert msg.content == "完成分析"
    assert msg.run_id == run.id

    assert batch.status == "completed"
    assert run.status == "completed"


async def test_batch_reject_publishes_nothing_and_run_fails(
    db_session, user_factory, session_factory, run_factory
) -> None:
    user = await user_factory()
    session = await session_factory(user.id)
    run = await run_factory(session.id, user.id)
    service = ArtifactService(db_session)
    await _submit(run, db_session)

    artifact_a, draft_a, _ = await _make_draft(
        service, session_id=session.id, user_id=user.id, run_id=run.id,
        brand="瑞幸", payload=PAYLOAD_V1,
    )
    _, draft_b, _ = await _make_draft(
        service, session_id=session.id, user_id=user.id, run_id=run.id,
        brand="库迪", payload=PAYLOAD_V1,
    )

    gateway = _make_gateway(db_session, [APPROVE_JSON, REJECT_JSON])
    driver = ReviewerDriver(db_session, gateway, worker_id="worker")
    batch = await driver.create_batch(
        parent_run_id=run.id, draft_ids=(draft_a.id, draft_b.id), completion_text="完成分析"
    )
    items = await _items_for(db_session, batch.id)

    for item in items:
        await driver.review_item(parent_run=run, item=item, user_question="分析")

    # 任一 reject → 整批失败，不产生部分发布
    assert run.status == "failed"
    assert batch.status == "failed"
    assert len((await db_session.scalars(select(AgentArtifactVersion))).all()) == 0
    assert artifact_a.status == "failed"
    draft_b_row = await db_session.get(type(draft_b), draft_b.id)
    assert draft_b_row is not None and draft_b_row.status == "failed"

    # publish 被阻止且仍无任何版本
    with pytest.raises(Exception):
        await service.publish_batch(batch.id, worker_id="worker")
    assert len((await db_session.scalars(select(AgentArtifactVersion))).all()) == 0


async def test_revised_draft_re_reviewed_and_prior_approval_reused(
    db_session, user_factory, session_factory, run_factory
) -> None:
    user = await user_factory()
    session = await session_factory(user.id)
    run = await run_factory(session.id, user.id)
    service = ArtifactService(db_session)
    await _submit(run, db_session)

    _, draft_a, _ = await _make_draft(
        service, session_id=session.id, user_id=user.id, run_id=run.id,
        brand="瑞幸", payload=PAYLOAD_V1,
    )
    _, draft_b, _ = await _make_draft(
        service, session_id=session.id, user_id=user.id, run_id=run.id,
        brand="库迪", payload=PAYLOAD_V1,
    )

    # 第一轮：A approve，B revise
    gateway = _make_gateway(db_session, [APPROVE_JSON, REVISE_JSON])
    driver = ReviewerDriver(db_session, gateway, worker_id="worker")
    batch = await driver.create_batch(
        parent_run_id=run.id, draft_ids=(draft_a.id, draft_b.id), completion_text="完成分析"
    )
    items = await _items_for(db_session, batch.id)
    item_a = next(i for i in items if i.artifact_id == draft_a.artifact_id)
    item_b = next(i for i in items if i.artifact_id == draft_b.artifact_id)

    r_a = await driver.review_item(parent_run=run, item=item_a, user_question="分析")
    r_b = await driver.review_item(parent_run=run, item=item_b, user_question="分析")
    assert r_a.decision == "approve"
    assert r_b.decision == "revise"
    assert item_b.status == "revise"

    # 主 Agent 修订 B → 新 Revision
    _, revision_b2 = await service.update_draft(
        run_id=run.id, draft_id=draft_b.id, payload=PAYLOAD_V2, evidence_refs=[]
    )

    # 第二轮：只复核 B；A 的 approve 复用（未修改）
    gateway2 = _make_gateway(db_session, [APPROVE_JSON])
    driver2 = ReviewerDriver(db_session, gateway2, worker_id="worker")
    results = await driver2.review_pending(parent_run=run, batch=batch, user_question="分析")
    assert [r.review_item_id for r in results] == [item_b.id]
    assert results[0].decision == "approve"
    assert results[0].draft_revision_id == revision_b2.id

    versions = await service.publish_batch(batch.id, worker_id="worker")
    assert len(versions) == 2
    assert run.status == "completed"


async def test_modified_approved_draft_must_be_re_reviewed(
    db_session, user_factory, session_factory, run_factory
) -> None:
    user = await user_factory()
    session = await session_factory(user.id)
    run = await run_factory(session.id, user.id)
    service = ArtifactService(db_session)
    await _submit(run, db_session)

    _, draft_a, _ = await _make_draft(
        service, session_id=session.id, user_id=user.id, run_id=run.id,
        brand="瑞幸", payload=PAYLOAD_V1,
    )
    _, draft_b, _ = await _make_draft(
        service, session_id=session.id, user_id=user.id, run_id=run.id,
        brand="库迪", payload=PAYLOAD_V1,
    )

    gateway = _make_gateway(db_session, [APPROVE_JSON, APPROVE_JSON])
    driver = ReviewerDriver(db_session, gateway, worker_id="worker")
    batch = await driver.create_batch(
        parent_run_id=run.id, draft_ids=(draft_a.id, draft_b.id), completion_text="完成分析"
    )
    items = await _items_for(db_session, batch.id)
    item_a = next(i for i in items if i.artifact_id == draft_a.artifact_id)
    item_b = next(i for i in items if i.artifact_id == draft_b.artifact_id)

    await driver.review_item(parent_run=run, item=item_a, user_question="分析")
    await driver.review_item(parent_run=run, item=item_b, user_question="分析")
    assert item_a.status == "approved"
    assert item_b.status == "approved"

    # A 在 approve 之后被修改 → 旧 approve 自动失效
    _, revision_a2 = await service.update_draft(
        run_id=run.id, draft_id=draft_a.id, payload=PAYLOAD_V2, evidence_refs=[]
    )

    gateway2 = _make_gateway(db_session, [APPROVE_JSON])
    driver2 = ReviewerDriver(db_session, gateway2, worker_id="worker")
    results = await driver2.review_pending(parent_run=run, batch=batch, user_question="分析")
    assert [r.review_item_id for r in results] == [item_a.id]
    assert results[0].draft_revision_id == revision_a2.id

    # B 未修改，approve 复用；最终两个版本都发布
    versions = await service.publish_batch(batch.id, worker_id="worker")
    assert len(versions) == 2


async def test_review_and_revision_counts_aggregate(
    db_session, user_factory, session_factory, run_factory
) -> None:
    user = await user_factory()
    session = await session_factory(user.id)
    run = await run_factory(session.id, user.id)
    service = ArtifactService(db_session)
    await _submit(run, db_session)

    _, draft_a, _ = await _make_draft(
        service, session_id=session.id, user_id=user.id, run_id=run.id,
        brand="瑞幸", payload=PAYLOAD_V1,
    )
    _, draft_b, _ = await _make_draft(
        service, session_id=session.id, user_id=user.id, run_id=run.id,
        brand="库迪", payload=PAYLOAD_V1,
    )

    gateway = _make_gateway(db_session, [APPROVE_JSON, REVISE_JSON])
    driver = ReviewerDriver(db_session, gateway, worker_id="worker")
    batch = await driver.create_batch(
        parent_run_id=run.id, draft_ids=(draft_a.id, draft_b.id), completion_text="完成分析"
    )
    items = await _items_for(db_session, batch.id)
    item_a = next(i for i in items if i.artifact_id == draft_a.artifact_id)
    item_b = next(i for i in items if i.artifact_id == draft_b.artifact_id)
    # 显式顺序：A approve，B revise（与网关 outcome 顺序一致）
    await driver.review_item(parent_run=run, item=item_a, user_question="分析")
    await driver.review_item(parent_run=run, item=item_b, user_question="分析")

    # 第一轮：2 次 Reviewer 调用、1 次 revise
    assert run.review_count == 2
    assert run.revision_count == 1
    draft_a_row = await db_session.get(type(draft_a), draft_a.id)
    draft_b_row = await db_session.get(type(draft_b), draft_b.id)
    assert draft_a_row is not None and draft_a_row.review_count == 1
    assert draft_b_row is not None and draft_b_row.review_count == 1

    # B 修订后第二轮只复核 B → 总 3 次调用、仍 1 次 revise
    await service.update_draft(run_id=run.id, draft_id=draft_b.id, payload=PAYLOAD_V2, evidence_refs=[])
    gateway2 = _make_gateway(db_session, [APPROVE_JSON])
    driver2 = ReviewerDriver(db_session, gateway2, worker_id="worker")
    await driver2.review_pending(parent_run=run, batch=batch, user_question="分析")

    assert run.review_count == 3
    assert run.revision_count == 1
    draft_b_row = await db_session.get(type(draft_b), draft_b.id)
    assert draft_b_row is not None and draft_b_row.review_count == 2

    # attempt 历史按 (review_item_id, attempt) 累计
    attempt_count = await db_session.scalar(
        select(func.count(ArtifactReviewAttempt.id)).where(
            ArtifactReviewAttempt.review_item_id == item_b.id
        )
    )
    assert attempt_count == 2
