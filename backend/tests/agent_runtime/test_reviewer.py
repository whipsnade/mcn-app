"""Reviewer 内部 Run 与三次调用/两次 revise 规则测试（设计 §12.3 / Task 13）。

覆盖：
1. approve → 原子发布成功；revise → Item 停留 reviewing / 要求修订；
2. 第 3 次 revise 映射为 reject：Run failed、不产生任何 Version；
3. 每次 Reviewer 调用都创建独立 internal 子 Run：run_kind=internal、
   visibility=internal、profile=artifact_reviewer_v1、parent_run_id=用户 Run；
   带自己的 Prompt 快照、Step、token 用量；不增加父 Run 的 decision_count /
   Attempt decision_count；
4. Reviewer 输入只含用户问题 + 不可变 Revision payload + 解析 lineage +
   允许 Schema + 已知限制；Reviewer 不注册任何 MCP 工具；
5. artifact_review_attempts 按 (review_item_id, attempt) 不可变记录每次调用。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.agent_artifacts.models import (
    AgentArtifactVersion,
    ArtifactReviewAttempt,
    ArtifactReviewItem,
)
from app.agent_artifacts.service import ArtifactBusy, ArtifactService
from app.agent_runtime.model_gateway import AgentModelGateway
from app.agent_runtime.models import (
    AgentMessage,
    AgentRun,
    AgentRunAttempt,
    AgentSession,
    AgentStep,
    AgentToolCall,
    EvidenceItem,
)
from app.agent_runtime.repository import AgentRunRepository
from app.agent_runtime.reviewer import (
    ReviewAttemptResult,
    ReviewOwnershipError,
    ReviewerDriver,
)
from app.agent_runtime.state import RunStatus
from app.model.prompt_logs import PromptLogEntry
from app.model.tencent_plan import TencentPlanAdapter

APPROVE_JSON = '{"decision":"approve","issues":[]}'
REVISE_JSON = (
    '{"decision":"revise","issues":[{"code":"missing_data","message":"需要补查声量数据"}]}'
)
REJECT_JSON = (
    '{"decision":"reject","issues":[{"code":"untrusted","message":"数字无法追溯"}]}'
)

# 无必需数字叶子的 payload：lineage 校验结果为空闭包，测试无需建 Evidence。
PAYLOAD_V1 = {"data": {"overview": {"brand": "瑞幸"}}}
PAYLOAD_V2 = {"data": {"overview": {"brand": "瑞幸", "note": "补查后"}}}

# 需要 lineage 的 payload + 对应 Evidence。
PAYLOAD_WITH_NUMBER = {"data": {"overview": {"total_volume": 100}}}
EVIDENCE_REFS = [
    {
        "artifact_path": "/data/overview/total_volume",
        "sources": [
            {
                "source_type": "evidence",
                "evidence_id": "e-1",
                "source_path": "/0/声量",
            }
        ],
    }
]


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


def _make_gateway(db_session, decision_jsons: list[str]) -> tuple[AgentModelGateway, FakeCompletions]:
    client = FakeCompletions(
        [stream_chunks(content_chunks=[j], reasoning_chunks=[None]) for j in decision_jsons]
    )
    adapter = TencentPlanAdapter(
        client=client,
        log_writer=_CaptureWriter(),
        stream_support_cache={},
    )
    return AgentModelGateway(adapter, db=db_session), client


async def _setup_run(db_session, user_factory) -> tuple[AgentRun, AgentRunAttempt]:
    """创建 running 用户 Run + 首条 Attempt，并持有一份活跃租约（worker）。"""
    user = await user_factory()
    now = utc_now()
    session = AgentSession(
        id=str(uuid4()),
        user_id=user.id,
        title="评审测试会话",
        status="active",
        created_at=now,
        updated_at=now,
    )
    db_session.add(session)
    await db_session.flush()
    run = AgentRun(
        id=str(uuid4()),
        session_id=session.id,
        user_id=user.id,
        run_kind="user",
        visibility="user",
        profile_name="session_analyst_v1",
        profile_version="v1",
        model="test-model",
        status="running",
        decision_count=0,
        review_count=0,
        revision_count=0,
        started_at=now,
    )
    db_session.add(run)
    await db_session.flush()
    attempt = AgentRunAttempt(
        id=str(uuid4()),
        run_id=run.id,
        attempt=1,
        started_at=now,
        decision_count=0,
        outcome="running",
    )
    db_session.add(attempt)
    await db_session.flush()
    repo = AgentRunRepository(db_session)
    await repo.claim_lease(run.id, "worker", 300)
    return run, attempt


async def _submit_review(repo: AgentRunRepository, run: AgentRun) -> None:
    await repo.transition(run.id, RunStatus.REVIEWING, worker_id="worker")


async def _make_evidence(
    db_session, run: AgentRun, *, evidence_id: str, raw_payload: Any
) -> EvidenceItem:
    """构造 Evidence 查询链（attempt → step → tool_call → evidence），供 lineage 解析。"""
    now = utc_now()
    # 复用父 Run 已有的 attempt（_setup_run 已创建 attempt=1），避免撞唯一约束。
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
    step = AgentStep(
        id=str(uuid4()),
        run_id=run.id,
        attempt_id=attempt.id,
        sequence=1,
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


async def _make_draft(
    service: ArtifactService,
    *,
    session_id: str,
    user_id: str,
    run_id: str,
    brand: str,
    payload: dict[str, Any],
    evidence_refs: list[dict[str, Any]] | None = None,
):
    return await service.create_or_get_draft(
        session_id=session_id,
        user_id=user_id,
        run_id=run_id,
        module="brand",
        business_fields={"brand": brand},
        schema_version="brand_report_v3",
        payload=payload,
        evidence_refs=evidence_refs,
        artifact_type="brand_report_v3",
    )


async def test_approve_on_first_call_then_publish(
    db_session, user_factory
) -> None:
    run, _ = await _setup_run(db_session, user_factory)
    await _submit_review(AgentRunRepository(db_session), run)
    service = ArtifactService(db_session)
    _, draft, _ = await _make_draft(
        service,
        session_id=run.session_id,
        user_id=run.user_id,
        run_id=run.id,
        brand="瑞幸",
        payload=PAYLOAD_V1,
        evidence_refs=[],
    )

    gateway, _ = _make_gateway(db_session, [APPROVE_JSON])
    driver = ReviewerDriver(db_session, gateway, worker_id="worker")
    batch = await driver.create_batch(
        parent_run_id=run.id, draft_ids=(draft.id,), completion_text="完成"
    )
    item = (
        await db_session.scalars(
            select(ArtifactReviewItem).where(ArtifactReviewItem.batch_id == batch.id)
        )
    ).one()

    result = await driver.review_item(parent_run=run, item=item, user_question="分析瑞幸品牌")
    assert isinstance(result, ReviewAttemptResult)
    assert result.decision == "approve"
    assert item.status == "approved"

    versions = await service.publish_batch(batch.id, worker_id="worker")
    assert len(versions) == 1
    assert versions[0].source_draft_revision_id == item.draft_revision_id

    draft_row = await db_session.get(type(draft), draft.id)
    assert draft_row is not None
    assert draft_row.status == "idle"
    assert draft_row.owner_run_id is None

    # assistant 消息只在发布后写入
    msg = await db_session.scalar(
        select(AgentMessage).where(AgentMessage.session_id == run.session_id)
    )
    assert msg is not None
    assert msg.role == "assistant"
    assert msg.content == "完成"
    assert msg.run_id == run.id

    assert run.status == "completed"


async def test_revise_keeps_item_reviewing_and_requests_revision(
    db_session, user_factory
) -> None:
    run, _ = await _setup_run(db_session, user_factory)
    await _submit_review(AgentRunRepository(db_session), run)
    service = ArtifactService(db_session)
    _, draft, _ = await _make_draft(
        service,
        session_id=run.session_id,
        user_id=run.user_id,
        run_id=run.id,
        brand="瑞幸",
        payload=PAYLOAD_V1,
        evidence_refs=[],
    )

    gateway, _ = _make_gateway(db_session, [REVISE_JSON])
    driver = ReviewerDriver(db_session, gateway, worker_id="worker")
    batch = await driver.create_batch(
        parent_run_id=run.id, draft_ids=(draft.id,), completion_text="完成"
    )
    item = (
        await db_session.scalars(
            select(ArtifactReviewItem).where(ArtifactReviewItem.batch_id == batch.id)
        )
    ).one()

    result = await driver.review_item(parent_run=run, item=item, user_question="分析瑞幸品牌")
    assert result.decision == "revise"
    assert item.status == "revise"

    # 尚未 publish：没有版本、没有 assistant 消息、Run 仍停留 reviewing
    assert run.status == "reviewing"
    assert len((await db_session.scalars(select(AgentArtifactVersion))).all()) == 0
    assert (
        await db_session.scalar(
            select(func.count(AgentMessage.id)).where(AgentMessage.session_id == run.session_id)
        )
    ) == 0


async def test_third_revise_is_treated_as_reject_and_run_fails(
    db_session, user_factory
) -> None:
    run, _ = await _setup_run(db_session, user_factory)
    await _submit_review(AgentRunRepository(db_session), run)
    service = ArtifactService(db_session)
    _, draft, _ = await _make_draft(
        service,
        session_id=run.session_id,
        user_id=run.user_id,
        run_id=run.id,
        brand="瑞幸",
        payload=PAYLOAD_V1,
        evidence_refs=[],
    )

    gateway, _ = _make_gateway(db_session, [REVISE_JSON, REVISE_JSON, REVISE_JSON])
    driver = ReviewerDriver(db_session, gateway, worker_id="worker")
    batch = await driver.create_batch(
        parent_run_id=run.id, draft_ids=(draft.id,), completion_text="完成"
    )
    item = (
        await db_session.scalars(
            select(ArtifactReviewItem).where(ArtifactReviewItem.batch_id == batch.id)
        )
    ).one()

    r1 = await driver.review_item(parent_run=run, item=item, user_question="分析瑞幸品牌")
    assert r1.decision == "revise"
    assert r1.attempt == 1
    assert item.status == "revise"

    # 主 Agent 修订 → 新 Revision，Item 改绑新 Revision 后再次送审
    _, revision2 = await service.update_draft(
        run_id=run.id, draft_id=draft.id, payload=PAYLOAD_V2, evidence_refs=[]
    )
    r2 = await driver.review_item(parent_run=run, item=item, user_question="分析瑞幸品牌")
    assert r2.decision == "revise"
    assert r2.attempt == 2
    assert r2.draft_revision_id == revision2.id
    assert item.status == "revise"

    _, revision3 = await service.update_draft(
        run_id=run.id, draft_id=draft.id, payload=PAYLOAD_V2, evidence_refs=[]
    )
    r3 = await driver.review_item(parent_run=run, item=item, user_question="分析瑞幸品牌")
    # 第 3 次仍输出 revise → 运行时按 reject 处理
    assert r3.decision == "reject"
    assert r3.attempt == 3
    assert r3.draft_revision_id == revision3.id
    assert item.status == "rejected"

    # reject 收口：Run failed、Draft failed、Batch failed、无任何版本
    assert run.status == "failed"
    assert batch.status == "failed"
    draft_row = await db_session.get(type(draft), draft.id)
    assert draft_row is not None
    assert draft_row.status == "failed"
    assert len((await db_session.scalars(select(AgentArtifactVersion))).all()) == 0

    with pytest.raises(Exception):
        await service.publish_batch(batch.id, worker_id="worker")
    assert len((await db_session.scalars(select(AgentArtifactVersion))).all()) == 0


async def test_each_review_call_creates_internal_child_run_audit(
    db_session, user_factory
) -> None:
    run, attempt = await _setup_run(db_session, user_factory)
    await _submit_review(AgentRunRepository(db_session), run)
    service = ArtifactService(db_session)
    _, draft, _ = await _make_draft(
        service,
        session_id=run.session_id,
        user_id=run.user_id,
        run_id=run.id,
        brand="瑞幸",
        payload=PAYLOAD_V1,
        evidence_refs=[],
    )

    gateway, _ = _make_gateway(db_session, [APPROVE_JSON])
    driver = ReviewerDriver(db_session, gateway, worker_id="worker")
    batch = await driver.create_batch(
        parent_run_id=run.id, draft_ids=(draft.id,), completion_text="完成"
    )
    item = (
        await db_session.scalars(
            select(ArtifactReviewItem).where(ArtifactReviewItem.batch_id == batch.id)
        )
    ).one()
    await driver.review_item(parent_run=run, item=item, user_question="分析瑞幸品牌")

    child_runs = (
        await db_session.scalars(select(AgentRun).where(AgentRun.parent_run_id == run.id))
    ).all()
    assert len(child_runs) == 1
    child = child_runs[0]
    assert child.run_kind == "internal"
    assert child.visibility == "internal"
    assert child.profile_name == "artifact_reviewer_v1"
    assert child.profile_version == "v1"
    assert child.parent_run_id == run.id
    assert child.session_id == run.session_id
    assert child.model == run.model
    assert child.prompt_snapshot_json is not None

    # 内部子 Run 有自己的 Step 与 token 用量
    step = await db_session.scalar(select(AgentStep).where(AgentStep.run_id == child.id))
    assert step is not None
    assert step.visibility == "internal"
    assert step.token_usage_json is not None
    assert step.output_json is not None

    # 不计入父 Run 决策阈值（decision_count 不变，attempt 也不变）
    assert run.decision_count == 0
    assert attempt.decision_count == 0
    # 但 review 审计值累计
    assert run.review_count == 1


async def test_reviewer_input_contract_no_mcp_and_resolved_lineage(
    db_session, user_factory
) -> None:
    run, _ = await _setup_run(db_session, user_factory)
    await _submit_review(AgentRunRepository(db_session), run)
    await _make_evidence(db_session, run, evidence_id="e-1", raw_payload=[{"声量": 100}])
    service = ArtifactService(db_session)
    _, draft, revision = await _make_draft(
        service,
        session_id=run.session_id,
        user_id=run.user_id,
        run_id=run.id,
        brand="瑞幸",
        payload=PAYLOAD_WITH_NUMBER,
        evidence_refs=EVIDENCE_REFS,
    )

    gateway, client = _make_gateway(db_session, [APPROVE_JSON])
    driver = ReviewerDriver(db_session, gateway, worker_id="worker")
    batch = await driver.create_batch(
        parent_run_id=run.id, draft_ids=(draft.id,), completion_text="完成"
    )
    item = (
        await db_session.scalars(
            select(ArtifactReviewItem).where(ArtifactReviewItem.batch_id == batch.id)
        )
    ).one()
    result = await driver.review_item(parent_run=run, item=item, user_question="为什么声量下降")

    # 发送给模型的 user 消息只含评审上下文
    assert len(client.calls) == 1
    user_msg = client.calls[0]["messages"][-1]
    assert user_msg["role"] == "user"
    context = json.loads(user_msg["content"])
    assert context["user_question"] == "为什么声量下降"
    assert context["draft_revision_id"] == revision.id
    assert context["payload"] == PAYLOAD_WITH_NUMBER
    # 解析后的 lineage 闭包含 Evidence 叶子
    lineage = context["lineage"]
    assert lineage["refs"][0]["artifact_path"] == "/data/overview/total_volume"
    assert lineage["refs"][0]["sources"][0]["evidence_id"] == "e-1"
    # 允许的 Schema + 已知限制
    assert context["schema"]["schema_version"] == "brand_report_v3"
    assert isinstance(context["limitations"], list)
    # 请求不带任何工具注册
    assert "tools" not in client.calls[0]

    # Reviewer 子 Run 无任何 MCP 工具调用
    child = await db_session.scalar(select(AgentRun).where(AgentRun.parent_run_id == run.id))
    assert child is not None
    assert (
        await db_session.scalar(
            select(func.count(AgentToolCall.id)).where(AgentToolCall.run_id == child.id)
        )
    ) == 0

    assert result.decision == "approve"


async def test_cancel_reviewing_frees_reviewing_draft_for_new_run(
    db_session, user_factory
) -> None:
    """父 Run 取消/系统失败时，cancel_reviewing 释放 reviewing Draft 供新 Run 接管。"""
    run_a, _ = await _setup_run(db_session, user_factory)
    await _submit_review(AgentRunRepository(db_session), run_a)
    service = ArtifactService(db_session)
    _, draft, _ = await _make_draft(
        service,
        session_id=run_a.session_id,
        user_id=run_a.user_id,
        run_id=run_a.id,
        brand="瑞幸",
        payload=PAYLOAD_V1,
        evidence_refs=[],
    )

    gateway, _ = _make_gateway(db_session, [APPROVE_JSON])
    driver = ReviewerDriver(db_session, gateway, worker_id="worker")
    await driver.create_batch(
        parent_run_id=run_a.id, draft_ids=(draft.id,), completion_text="完成"
    )
    assert draft.status == "reviewing"

    # 父 Run 在 reviewing 状态被取消（系统原因，非 reject）
    await driver.cancel_reviewing(run_id=run_a.id, draft_ids=(draft.id,), outcome="failed")
    # 幂等：owner 已释放后再次调用同一 hook 不报错
    await driver.cancel_reviewing(run_id=run_a.id, draft_ids=(draft.id,), outcome="failed")

    draft_row = await db_session.get(type(draft), draft.id)
    assert draft_row is not None
    assert draft_row.status == "failed"
    assert draft_row.owner_run_id is None

    # 新 Run（同一 session）可立即接管同一 Artifact，不再 artifact_busy，
    # Revision 在历史之上递增。
    now = utc_now()
    run_b = AgentRun(
        id=str(uuid4()),
        session_id=run_a.session_id,
        user_id=run_a.user_id,
        run_kind="user",
        visibility="user",
        profile_name="session_analyst_v1",
        profile_version="v1",
        model="test-model",
        status="running",
        decision_count=0,
        review_count=0,
        revision_count=0,
        started_at=now,
    )
    db_session.add(run_b)
    await db_session.flush()
    await AgentRunRepository(db_session).claim_lease(run_b.id, "worker", 300)

    artifact_b, draft_b, revision_b = await _make_draft(
        service,
        session_id=run_a.session_id,
        user_id=run_a.user_id,
        run_id=run_b.id,
        brand="瑞幸",
        payload=PAYLOAD_V2,
        evidence_refs=[],
    )
    assert artifact_b.id == draft.artifact_id
    assert draft_b.id == draft.id
    assert draft_b.owner_run_id == run_b.id
    assert draft_b.status == "drafting"
    assert revision_b.revision == 2

    # 防御：Draft 已被另一 Run 接管后，原 Run 再 cancel 必须 ArtifactBusy（防误释放）
    with pytest.raises(ArtifactBusy):
        await driver.cancel_reviewing(run_id=run_a.id, draft_ids=(draft.id,), outcome="failed")


async def test_review_item_rejects_mismatched_parent_run(
    db_session, user_factory
) -> None:
    """Item 所属 batch 的 parent_run 与传入的父 Run 不一致时必须拒绝。"""
    run_a, _ = await _setup_run(db_session, user_factory)
    await _submit_review(AgentRunRepository(db_session), run_a)
    service = ArtifactService(db_session)
    _, draft, _ = await _make_draft(
        service,
        session_id=run_a.session_id,
        user_id=run_a.user_id,
        run_id=run_a.id,
        brand="瑞幸",
        payload=PAYLOAD_V1,
        evidence_refs=[],
    )

    gateway, _ = _make_gateway(db_session, [APPROVE_JSON])
    driver = ReviewerDriver(db_session, gateway, worker_id="worker")
    batch = await driver.create_batch(
        parent_run_id=run_a.id, draft_ids=(draft.id,), completion_text="完成"
    )
    item = (
        await db_session.scalars(
            select(ArtifactReviewItem).where(ArtifactReviewItem.batch_id == batch.id)
        )
    ).one()

    run_b, _ = await _setup_run(db_session, user_factory)
    with pytest.raises(ReviewOwnershipError):
        await driver.review_item(parent_run=run_b, item=item, user_question="分析")

    # Item 未被误改，也没有产生任何 attempt
    assert item.status == "pending"
    assert (
        await db_session.scalar(
            select(func.count(ArtifactReviewAttempt.id)).where(
                ArtifactReviewAttempt.review_item_id == item.id
            )
        )
    ) == 0


async def test_review_attempts_recorded_immutably_per_item_attempt(
    db_session, user_factory
) -> None:
    run, _ = await _setup_run(db_session, user_factory)
    await _submit_review(AgentRunRepository(db_session), run)
    service = ArtifactService(db_session)
    _, draft, revision1 = await _make_draft(
        service,
        session_id=run.session_id,
        user_id=run.user_id,
        run_id=run.id,
        brand="瑞幸",
        payload=PAYLOAD_V1,
        evidence_refs=[],
    )

    gateway, _ = _make_gateway(db_session, [REVISE_JSON, APPROVE_JSON])
    driver = ReviewerDriver(db_session, gateway, worker_id="worker")
    batch = await driver.create_batch(
        parent_run_id=run.id, draft_ids=(draft.id,), completion_text="完成"
    )
    item = (
        await db_session.scalars(
            select(ArtifactReviewItem).where(ArtifactReviewItem.batch_id == batch.id)
        )
    ).one()

    r1 = await driver.review_item(parent_run=run, item=item, user_question="分析")
    assert r1.decision == "revise"
    _, revision2 = await service.update_draft(
        run_id=run.id, draft_id=draft.id, payload=PAYLOAD_V2, evidence_refs=[]
    )
    r2 = await driver.review_item(parent_run=run, item=item, user_question="分析")

    attempts = (
        await db_session.scalars(
            select(ArtifactReviewAttempt)
            .where(ArtifactReviewAttempt.review_item_id == item.id)
            .order_by(ArtifactReviewAttempt.attempt)
        )
    ).all()
    assert len(attempts) == 2
    a1, a2 = attempts
    assert (a1.attempt, a1.decision, a1.draft_revision_id) == (1, "revise", revision1.id)
    assert (a2.attempt, a2.decision, a2.draft_revision_id) == (2, "approve", revision2.id)
    assert a1.review_run_id == r1.review_run_id
    assert a2.review_run_id == r2.review_run_id
    assert a1.issues_json is not None
    assert a2.issues_json is None or a2.issues_json == []

    # 唯一约束 (review_item_id, attempt)：重复插入同 attempt 会被拒绝
    dup = ArtifactReviewAttempt(
        id=str(uuid4()),
        review_item_id=item.id,
        attempt=1,
        draft_revision_id=revision1.id,
        review_run_id=r1.review_run_id,
        decision="approve",
        issues_json=None,
        created_at=utc_now(),
    )
    db_session.add(dup)
    with pytest.raises(IntegrityError):
        await db_session.flush()
