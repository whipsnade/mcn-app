"""独立 Artifact Reviewer 驱动（设计 §12.3 / §八 / Task 13）。

每次 Reviewer 调用都创建一个 ``run_kind=internal``、``visibility=internal``、
Profile=``artifact_reviewer_v1`` 的子 ``agent_runs``，``parent_run_id`` 指向
用户 Run。Reviewer 只读不可变 ``artifact_draft_revisions`` + 解析后的 lineage
+ 允许的 Artifact Schema + 已知限制，**不注册任何 MCP 工具**。

决策复用 Task 6 ``AgentModelGateway.decide()`` 的参数化路径：``decision_root``
是 ``ReviewDecision``（approve/revise/reject，独立于四种动作协议），输出 Schema
由适配器严格校验与修复。

三次调用 / 两次 revise 规则（§12.3）：
- 前两次可返回 approve/revise/reject；
- 第 3 次仍输出 revise 时运行时按 reject 处理；
- reject 必须以 Artifact failed、Run failed 收口，整个 batch 失败且不产生部分
  发布；只有 approve 可以发布。

Reviewer 调用不计入父 Run 的 Attempt 决策阈值；只累计 review/revision 审计值。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, RootModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_artifacts.lineage import (
    DbLineageLoader,
    LineageError,
    LineageOwner,
    validate_and_freeze_lineage,
)
from app.agent_artifacts.models import (
    AgentArtifact,
    ArtifactDraft,
    ArtifactDraftRevision,
    ArtifactReviewAttempt,
    ArtifactReviewBatch,
    ArtifactReviewItem,
)
from app.agent_artifacts.payloads import TYPED_PAYLOAD_BY_SCHEMA
from app.agent_artifacts.service import ArtifactService
from app.agent_runtime.model_gateway import AgentModelGateway
from app.agent_runtime.models import AgentRun
from app.agent_runtime.profiles import get_profile
from app.agent_runtime.repository import AgentRunRepository
from app.agent_runtime.state import RunStatus
from app.model.contracts import ChatMessage

# 单个 Item 最多三次 Reviewer 调用（§12.3：最多两次 revise）。
MAX_REVIEW_ATTEMPTS = 3


class ReviewIssue(BaseModel):
    """一条结构化复核问题（revise 的返回内容）。"""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=2000)
    paths: tuple[str, ...] = Field(default_factory=tuple)


class ReviewDecision(BaseModel):
    """Reviewer 决策：approve / revise / reject，独立于四种动作协议。"""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "revise", "reject"]
    issues: tuple[ReviewIssue, ...] = Field(default_factory=tuple)


class _ReviewDecisionRoot(RootModel[ReviewDecision]):
    pass


# 作为 ``AgentModelGateway.decide(decision_root=...)`` 的输出 Schema。
REVIEW_DECISION_ROOT: type[RootModel[ReviewDecision]] = _ReviewDecisionRoot


class ReviewLimitExceeded(RuntimeError):
    """Item 的复核次数已达上限（3 次）仍被再次送审，属引擎编排错误。"""


@dataclass(frozen=True)
class ReviewAttemptResult:
    """一次 Reviewer 调用的结果摘要（含 attempt 与内部 Run 引用）。"""

    decision: str
    attempt: int
    review_run_id: str
    review_item_id: str
    draft_revision_id: str


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class ReviewerDriver:
    """Reviewer 生命周期驱动：批次创建 + 单 Item 复核 + 整批发布前复核。

    内部 Run 的创建模式与 AgentRun 一致（含 Attempt / Step / token 审计），
    但 Reviewer 永不调用工具，因此不会产生任何 ``agent_tool_calls`` 行。
    ``worker_id`` 用于把父 Run 状态迁移到 reviewing→completed/failed（引擎持有
    父 Run 租约时以同一 worker 传入）。
    """

    def __init__(
        self,
        db: AsyncSession,
        gateway: AgentModelGateway,
        *,
        worker_id: str = "reviewer",
    ) -> None:
        self.db = db
        self.gateway = gateway
        self.worker_id = worker_id
        self._service = ArtifactService(db)
        self._repo = AgentRunRepository(db)

    # -- 批次创建 ----------------------------------------------------------------

    async def create_batch(
        self,
        *,
        parent_run_id: str,
        draft_ids: tuple[str, ...],
        completion_text: str,
    ) -> ArtifactReviewBatch:
        """为一个用户 Run 创建唯一 Review Batch 及其 Item，并把 Draft 置为 reviewing。

        每个 Item 绑定该 Draft 的**当前**不可变 Revision；Draft 后续被修改时由
        ``review_item`` 在下次调用前改绑新 Revision 并使旧 approve 失效。
        """
        batch = ArtifactReviewBatch(
            id=str(uuid4()),
            parent_run_id=parent_run_id,
            status="pending",
            completion_text=completion_text,
            created_at=_utcnow(),
        )
        self.db.add(batch)
        await self.db.flush()
        for draft_id in draft_ids:
            draft = await self.db.get(ArtifactDraft, draft_id)
            if draft is None:
                raise LookupError(f"draft {draft_id!r} not found")
            # working head 置为 reviewing（owner 必须是本 Run）。
            await self._service.mark_draft_reviewing(parent_run_id, draft.id)
            current_rev = await self.db.scalar(
                select(ArtifactDraftRevision).where(
                    ArtifactDraftRevision.draft_id == draft.id,
                    ArtifactDraftRevision.revision == draft.current_revision,
                )
            )
            if current_rev is None:  # pragma: no cover - current_revision 必然指向已存在 Revision
                raise LookupError(
                    f"revision {draft.current_revision} for draft {draft.id!r} not found"
                )
            item = ArtifactReviewItem(
                id=str(uuid4()),
                batch_id=batch.id,
                artifact_id=draft.artifact_id,
                draft_revision_id=current_rev.id,
                status="pending",
            )
            self.db.add(item)
        await self.db.flush()
        return batch

    # -- 复核 --------------------------------------------------------------------

    async def review_item(
        self,
        *,
        parent_run: AgentRun,
        item: ArtifactReviewItem,
        user_question: str,
    ) -> ReviewAttemptResult:
        """对单个 Item 执行一次 Reviewer 调用（一个内部子 Run + 一条 attempt 记录）。

        - Draft 已修改（Item 绑定旧 Revision）时先改绑当前 Revision，旧 approve 失效；
        - 第 3 次调用仍输出 revise → 按 reject 处理；
        - reject → 整批 failed、全部 Draft/Artifact failed、父 Run failed。
        """
        now = _utcnow()
        batch = await self.db.get(ArtifactReviewBatch, item.batch_id)
        if batch is None:
            raise LookupError(f"review batch {item.batch_id!r} not found")
        draft = await self.db.scalar(
            select(ArtifactDraft).where(ArtifactDraft.artifact_id == item.artifact_id)
        )
        if draft is None:
            raise LookupError(f"draft for artifact {item.artifact_id!r} not found")
        current_rev = await self.db.scalar(
            select(ArtifactDraftRevision).where(
                ArtifactDraftRevision.draft_id == draft.id,
                ArtifactDraftRevision.revision == draft.current_revision,
            )
        )
        if current_rev is None:  # pragma: no cover
            raise LookupError(
                f"revision {draft.current_revision} for draft {draft.id!r} not found"
            )

        # Draft 修改后：Item 改绑新 Revision，先前 approve 自动失效。
        if item.draft_revision_id != current_rev.id:
            item.draft_revision_id = current_rev.id
            item.status = "pending"

        # 复核次数上限（每 Item 独立）。
        attempt_count = await self.db.scalar(
            select(func.count(ArtifactReviewAttempt.id)).where(
                ArtifactReviewAttempt.review_item_id == item.id
            )
        )
        next_attempt = (attempt_count or 0) + 1
        if next_attempt > MAX_REVIEW_ATTEMPTS:
            raise ReviewLimitExceeded(
                f"review item {item.id!r} already reached {MAX_REVIEW_ATTEMPTS} attempts"
            )

        # 创建独立 internal 子 Run（自己的 Attempt / Step / token 审计）。
        review_run = self._new_review_run(parent_run, now)
        self.db.add(review_run)
        await self.db.flush()
        review_attempt = await self._repo.begin_attempt(review_run.id)

        # 只读不可变 Revision + 解析 lineage + Schema + 限制，构造评审上下文。
        context = await self._build_context(parent_run, current_rev, user_question)
        review_run.prompt_snapshot_json = context

        decision = await self.gateway.decide(
            run=review_run,
            attempt_id=review_attempt.id,
            profile=get_profile("artifact_reviewer_v1"),
            messages=[ChatMessage(role="user", content=json.dumps(context, ensure_ascii=False))],
            thinking_sink=None,
            step_sequence=1,
            purpose="agent_loop",
            template_name="artifact_reviewer_v1",
            decision_root=REVIEW_DECISION_ROOT,
        )

        # 内部子 Run 收口（一次性调用，不参与用户可见状态机）。
        review_run.status = "completed"
        review_run.completed_at = _utcnow()
        review_run.decision_count = 1
        review_attempt.outcome = "completed"
        review_attempt.ended_at = _utcnow()
        review_attempt.decision_count = 1

        # 三次调用 / 两次 revise：第 3 次 revise → reject。
        raw_decision = decision.decision
        effective = "reject" if (next_attempt >= 3 and raw_decision == "revise") else raw_decision

        attempt = ArtifactReviewAttempt(
            id=str(uuid4()),
            review_item_id=item.id,
            attempt=next_attempt,
            draft_revision_id=current_rev.id,
            review_run_id=review_run.id,
            decision=effective,
            issues_json=[issue.model_dump() for issue in decision.issues] or None,
            created_at=now,
        )
        self.db.add(attempt)

        item.status = {
            "approve": "approved",
            "revise": "revise",
            "reject": "rejected",
        }[effective]

        # 审计计数：review_count=调用次数，revision_count=revise 次数。
        parent_run.review_count += 1
        draft.review_count += 1
        if effective == "revise":
            parent_run.revision_count += 1

        if effective == "reject":
            await self._finalize_reject(parent_run=parent_run, batch=batch)

        await self.db.flush()
        return ReviewAttemptResult(
            decision=effective,
            attempt=next_attempt,
            review_run_id=review_run.id,
            review_item_id=item.id,
            draft_revision_id=current_rev.id,
        )

    async def review_pending(
        self,
        *,
        parent_run: AgentRun,
        batch: ArtifactReviewBatch,
        user_question: str,
    ) -> list[ReviewAttemptResult]:
        """复核一轮：只审需要复核的 Item（未 approve 或绑定已过期）。

        未修改且已 approve 的 Revision 直接复用，不重新审核。
        """
        items = (
            await self.db.scalars(
                select(ArtifactReviewItem).where(ArtifactReviewItem.batch_id == batch.id)
            )
        ).all()
        results: list[ReviewAttemptResult] = []
        for item in items:
            if not await self._needs_review(item):
                continue
            result = await self.review_item(
                parent_run=parent_run, item=item, user_question=user_question
            )
            results.append(result)
            if result.decision == "reject":
                break
        return results

    # -- 内部工具 -----------------------------------------------------------------

    def _new_review_run(self, parent_run: AgentRun, now: datetime) -> AgentRun:
        return AgentRun(
            id=str(uuid4()),
            session_id=parent_run.session_id,
            user_id=parent_run.user_id,
            parent_run_id=parent_run.id,
            run_kind="internal",
            visibility="internal",
            profile_name=get_profile("artifact_reviewer_v1").full_name,
            profile_version=get_profile("artifact_reviewer_v1").version,
            model=parent_run.model,
            prompt_snapshot_json=None,
            status="queued",
            decision_count=0,
            review_count=0,
            revision_count=0,
        )

    async def _needs_review(self, item: ArtifactReviewItem) -> bool:
        draft = await self.db.scalar(
            select(ArtifactDraft).where(ArtifactDraft.artifact_id == item.artifact_id)
        )
        if draft is None:
            return False
        current_rev = await self.db.scalar(
            select(ArtifactDraftRevision).where(
                ArtifactDraftRevision.draft_id == draft.id,
                ArtifactDraftRevision.revision == draft.current_revision,
            )
        )
        if current_rev is None:
            return False
        if item.draft_revision_id != current_rev.id:
            return True
        return item.status != "approved"

    async def _build_context(
        self,
        parent_run: AgentRun,
        revision: ArtifactDraftRevision,
        user_question: str,
    ) -> dict[str, Any]:
        payload = revision.payload_json or {}
        try:
            frozen = await validate_and_freeze_lineage(
                payload=payload,
                refs=revision.evidence_refs_json or [],
                owner=LineageOwner(
                    user_id=parent_run.user_id,
                    session_id=parent_run.session_id,
                    run_id=parent_run.id,
                ),
                loader=DbLineageLoader(self.db),
            )
            lineage: dict[str, Any] = frozen.model_dump()
        except LineageError:
            # 提交时应已通过 lineage 校验；此处兜底给 Reviewer 原始引用，不阻塞复核。
            lineage = {"resolved": False, "refs": revision.evidence_refs_json or []}
        return {
            "user_question": user_question,
            "draft_revision_id": revision.id,
            "revision": revision.revision,
            "schema_version": revision.schema_version,
            "payload": payload,
            "lineage": lineage,
            "schema": self._schema_for(revision.schema_version),
            "limitations": payload.get("limitations", []),
        }

    @staticmethod
    def _schema_for(schema_version: str) -> dict[str, Any]:
        payload_cls = TYPED_PAYLOAD_BY_SCHEMA.get(schema_version)
        return {
            "schema_version": schema_version,
            "json_schema": (
                payload_cls.model_json_schema() if payload_cls is not None else None
            ),
        }

    async def _finalize_reject(
        self, *, parent_run: AgentRun, batch: ArtifactReviewBatch
    ) -> None:
        """reject 收口：整批 failed、全部 Draft/Artifact failed、父 Run failed。"""
        now = _utcnow()
        batch.status = "failed"
        batch.completed_at = now
        items = (
            await self.db.scalars(
                select(ArtifactReviewItem).where(ArtifactReviewItem.batch_id == batch.id)
            )
        ).all()
        for item in items:
            draft = await self.db.scalar(
                select(ArtifactDraft).where(ArtifactDraft.artifact_id == item.artifact_id)
            )
            if draft is not None:
                await self._service.release_draft(draft.id, outcome="failed")
            artifact = await self.db.get(AgentArtifact, item.artifact_id)
            if artifact is not None:
                artifact.status = "failed"
                artifact.updated_at = now
        await self._repo.transition(
            parent_run.id, RunStatus.FAILED, worker_id=self.worker_id
        )


__all__ = [
    "MAX_REVIEW_ATTEMPTS",
    "REVIEW_DECISION_ROOT",
    "ReviewAttemptResult",
    "ReviewDecision",
    "ReviewIssue",
    "ReviewLimitExceeded",
    "ReviewerDriver",
]
