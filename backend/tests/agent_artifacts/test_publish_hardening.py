"""发布事务强类型边界测试（v3 加固 §5.6 / A5）。

覆盖：
1. ``ArtifactLineageFreezer``：发布事务重算 ``validate_and_freeze_lineage``，
   把 Evidence 传递闭包写入 Version ``lineage_snapshot_json``（菱形去重、
   跨层级展开），``evidence_refs_json`` 原样保留模型直接引用；
2. 发布事务内锁定 Revision 后再次强类型校验：旧 Draft/旁路写入的非法
   payload 阻断整批发布（``ArtifactPayloadInvalid``，不产生任何 Version）；
3. ``data_status`` 取校验后 payload 的真实值（不再缺省 "complete"）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.agent_artifacts.models import (
    AgentArtifactVersion,
    ArtifactReviewBatch,
    ArtifactReviewItem,
)
from app.agent_artifacts.service import ArtifactPayloadInvalid, ArtifactService
from app.agent_runtime.models import (
    AgentRunAttempt,
    AgentStep,
    AgentToolCall,
    EvidenceItem,
)
from app.agent_runtime.repository import AgentRunRepository, utc_now
from app.agent_runtime.state import RunStatus

from tests.agent_artifacts.payload_fixtures import (
    insight_metric_payload,
    insight_metric_refs,
    insight_payload,
)


def _two_card_payload() -> dict[str, Any]:
    """两个 metric 数字叶子的合法 insight payload（两张卡各需一条 lineage）。"""
    return insight_payload(
        blocks=[
            {
                "block_type": "metric_grid",
                "title": "指标",
                "cards": [
                    {"key": "volume", "label": "声量", "value": 200},
                    {"key": "engagement", "label": "互动", "value": 300},
                ],
            }
        ]
    )


def _restricted_payload() -> dict[str, Any]:
    payload = insight_payload()
    payload["data_status"] = "restricted"
    payload["availability"]["blocks"] = {"status": "partial", "reason_codes": ["data_partial"]}
    payload["limitations"] = [
        {"code": "L_BLOCKS", "message": "部分内容缺失", "affected_paths": ["blocks"]}
    ]
    return payload


async def _make_evidence(
    db_session, run, *, evidence_id: str, raw_payload: Any
) -> EvidenceItem:
    """构造 Evidence 查询链（attempt → step → tool_call → evidence），供 lineage 解析。"""
    now = utc_now()
    attempt = await db_session.scalar(
        select(AgentRunAttempt).where(
            AgentRunAttempt.run_id == run.id, AgentRunAttempt.attempt == 1
        )
    )
    if attempt is None:
        attempt = AgentRunAttempt(
            id=str(uuid4()),
            run_id=run.id,
            attempt=1,
            started_at=now,
            decision_count=0,
            outcome="completed",
        )
        db_session.add(attempt)
        await db_session.flush()
    max_sequence = await db_session.scalar(
        select(func.max(AgentStep.sequence)).where(AgentStep.run_id == run.id)
    )
    step = AgentStep(
        id=str(uuid4()),
        run_id=run.id,
        attempt_id=attempt.id,
        sequence=(max_sequence or 0) + 1,
        step_type="tool_call",
        status="completed",
        visibility="internal",
        created_at=now,
    )
    db_session.add(step)
    await db_session.flush()
    tool_call = AgentToolCall(
        id=str(uuid4()),
        run_id=run.id,
        step_id=step.id,
        logical_call_id=str(uuid4()),
        service="internal",
        internal_tool_name="rank_kols",
        arguments_json={},
        arguments_hash="deadbeef",
        status="settled",
        started_at=now,
        completed_at=now,
    )
    db_session.add(tool_call)
    await db_session.flush()
    evidence = EvidenceItem(
        id=evidence_id,
        session_id=run.session_id,
        run_id=run.id,
        tool_call_id=tool_call.id,
        source_type="mcp",
        source_name="datatap",
        raw_payload_json=raw_payload,
        normalized_preview_json=None,
        payload_hash="beef",
        collected_at=now,
        availability_status="available",
    )
    db_session.add(evidence)
    await db_session.flush()
    return evidence


async def _setup_reviewing(db_session, user_factory, session_factory, run_factory):
    user = await user_factory()
    session = await session_factory(user.id)
    run = await run_factory(session.id, user.id)
    repo = AgentRunRepository(db_session)
    await repo.claim_lease(run.id, "worker", 300)
    await repo.transition(run.id, RunStatus.REVIEWING, worker_id="worker")
    return user, session, run, ArtifactService(db_session)


async def _approve_and_publish(
    db_session, service: ArtifactService, run, drafts: list
) -> list[AgentArtifactVersion]:
    """手工构造全部 approved 的 Batch 并发布（绕过 Reviewer 调用）。"""
    batch = ArtifactReviewBatch(
        id=str(uuid4()),
        parent_run_id=run.id,
        status="pending",
        completion_text="完成",
        created_at=utc_now(),
    )
    db_session.add(batch)
    await db_session.flush()
    for draft, revision in drafts:
        db_session.add(
            ArtifactReviewItem(
                id=str(uuid4()),
                batch_id=batch.id,
                artifact_id=draft.artifact_id,
                draft_revision_id=revision.id,
                status="approved",
            )
        )
    await db_session.flush()
    return await service.publish_batch(batch.id, worker_id="worker")


async def test_publish_freezes_lineage_closure_into_version(
    db_session, user_factory, session_factory, run_factory
) -> None:
    """发布 Version 携带冻结闭包：artifact 来源递归展开为 Evidence 叶子、
    菱形共享去重；evidence_refs_json 保留模型直接引用。"""
    user, session, run, service = await _setup_reviewing(
        db_session, user_factory, session_factory, run_factory
    )
    await _make_evidence(db_session, run, evidence_id="e-1", raw_payload=[{"声量": 100}])
    await _make_evidence(db_session, run, evidence_id="e-2", raw_payload=[{"互动": 300}])

    # 父已发布 Version：payload 数字叶子 → e-1。
    parent_artifact, parent_draft, parent_rev = await service.create_or_get_draft(
        session_id=session.id,
        user_id=user.id,
        run_id=run.id,
        module="insight",
        business_fields={"parent_artifact_version_id": "pv-root", "question": "父问题"},
        schema_version="insight_board_v1",
        payload=insight_metric_payload(value=100),
        evidence_refs=insight_metric_refs("e-1"),
        artifact_type="insight_board_v1",
    )
    parent_version = AgentArtifactVersion(
        id=str(uuid4()),
        artifact_id=parent_artifact.id,
        version=1,
        source_run_id=run.id,
        source_draft_revision_id=parent_rev.id,
        schema_version="insight_board_v1",
        payload_json=insight_metric_payload(value=100),
        evidence_refs_json=insight_metric_refs("e-1"),
        review_json=None,
        data_status="complete",
        created_at=datetime.now(UTC).replace(tzinfo=None),
    )
    db_session.add(parent_version)
    await db_session.flush()

    # 子 Draft：卡 0 经父 Version 递归到 e-1；卡 1 同时经父 Version（菱形共享）
    # 与直接 Evidence e-2。
    child_refs = [
        {
            "artifact_path": "/data/0/cards/0/value",
            "sources": [
                {
                    "source_type": "artifact",
                    "artifact_version_id": parent_version.id,
                    "source_path": "/data/0/cards/0/value",
                }
            ],
        },
        {
            "artifact_path": "/data/0/cards/1/value",
            "sources": [
                {
                    "source_type": "artifact",
                    "artifact_version_id": parent_version.id,
                    "source_path": "/data/0/cards/0/value",
                },
                {"source_type": "evidence", "evidence_id": "e-2", "source_path": "/0/互动"},
            ],
        },
    ]
    _, child_draft, child_rev = await service.create_or_get_draft(
        session_id=session.id,
        user_id=user.id,
        run_id=run.id,
        module="insight",
        business_fields={
            "parent_artifact_version_id": parent_version.id,
            "question": "子问题",
        },
        schema_version="insight_board_v1",
        payload=_two_card_payload(),
        evidence_refs=child_refs,
        artifact_type="insight_board_v1",
        parent_artifact_id=parent_artifact.id,
        parent_artifact_version_id=parent_version.id,
    )

    versions = await _approve_and_publish(db_session, service, run, [(child_draft, child_rev)])
    assert len(versions) == 1
    version = versions[0]

    # evidence_refs_json 原样保留（模型直接引用，含 artifact 来源）。
    assert version.evidence_refs_json == child_refs
    # lineage_snapshot_json 是发布时冻结的传递闭包：全部展开为 Evidence 叶子。
    snapshot = version.lineage_snapshot_json
    assert snapshot is not None
    by_path = {ref["artifact_path"]: ref for ref in snapshot["refs"]}
    card0 = by_path["/data/0/cards/0/value"]
    assert [(s["evidence_id"], s["source_path"]) for s in card0["sources"]] == [
        ("e-1", "/0/声量")
    ]
    card1 = by_path["/data/0/cards/1/value"]
    assert [(s["evidence_id"], s["source_path"]) for s in card1["sources"]] == [
        ("e-1", "/0/声量"),
        ("e-2", "/0/互动"),
    ]
    # 闭包自包含：来源只剩 evidence 叶子（payload_hash 一并冻结）。
    for ref in snapshot["refs"]:
        for source in ref["sources"]:
            assert set(source) == {"evidence_id", "source_path", "payload_hash"}
            assert source["payload_hash"] == "beef"


async def test_publish_revalidates_payload_and_blocks_legacy_invalid_draft(
    db_session, user_factory, session_factory, run_factory
) -> None:
    """旁路写入的非法 Revision payload：发布事务内二次校验阻断整批（不写任何行）。"""
    user, session, run, service = await _setup_reviewing(
        db_session, user_factory, session_factory, run_factory
    )
    _, draft, revision = await service.create_or_get_draft(
        session_id=session.id,
        user_id=user.id,
        run_id=run.id,
        module="insight",
        business_fields={"parent_artifact_version_id": "pv-1", "question": "旧数据"},
        schema_version="insight_board_v1",
        payload=insight_payload(),
        evidence_refs=[],
        artifact_type="insight_board_v1",
    )
    # 模拟 A5 前落库/旁路写入的非法 payload（绕过 create 校验直接改 Revision）。
    revision.payload_json = {"data": {"overview": {"total_volume": 100}}}
    await db_session.flush()

    with pytest.raises(ArtifactPayloadInvalid) as excinfo:
        await _approve_and_publish(db_session, service, run, [(draft, revision)])
    assert excinfo.value.code == "artifact_payload_invalid"
    assert (
        await db_session.scalar(select(func.count(AgentArtifactVersion.id)))
    ) == 0


async def test_publish_data_status_comes_from_validated_payload(
    db_session, user_factory, session_factory, run_factory
) -> None:
    """restricted payload 发布后 Version.data_status 必须是 restricted（不再缺省 complete）。"""
    user, session, run, service = await _setup_reviewing(
        db_session, user_factory, session_factory, run_factory
    )
    _, draft, revision = await service.create_or_get_draft(
        session_id=session.id,
        user_id=user.id,
        run_id=run.id,
        module="insight",
        business_fields={"parent_artifact_version_id": "pv-1", "question": "受限"},
        schema_version="insight_board_v1",
        payload=_restricted_payload(),
        evidence_refs=[],
        artifact_type="insight_board_v1",
    )
    versions = await _approve_and_publish(db_session, service, run, [(draft, revision)])
    assert len(versions) == 1
    assert versions[0].data_status == "restricted"
    # 空闭包也落快照（无必需数字叶子）。
    assert versions[0].lineage_snapshot_json == {"refs": []}
