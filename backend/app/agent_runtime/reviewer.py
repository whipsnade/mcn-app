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
  发布；只有 approve 可以发布。整批/Artifact 的清理由 Reviewer 完成，父 Run
  的终态迁移由引擎经终态事务边界（``AgentEventStream.settle_terminal``）
  与终态事件一体提交（H1）。

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
from app.agent_artifacts.service import ArtifactBusy, ArtifactService
from app.agent_runtime.model_gateway import AgentModelGateway
from app.agent_runtime.models import AgentRun
from app.agent_runtime.profiles import get_profile
from app.agent_runtime.repository import AgentRunRepository
from app.model.contracts import ChatMessage, ModelPlanInvalidError
from app.runtime_config.crypto import RuntimeConfigError
from app.runtime_config.schemas import RuntimeConfigSnapshot
from app.runtime_config.service import RuntimeConfigService

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


class ReviewOwnershipError(ValueError):
    """Item 所属 batch 的 parent_run 与传入的父 Run 不一致，拒绝复核。"""


class ReviewBatchDraftSetMismatch(ValueError):
    """复用既有 Batch 时提交的 Draft 集合与首次冻结的集合不一致（§5.7）。

    首次 ``submit_review`` 创建 Batch 后冻结 Draft ID 集合与 completion_text；
    后续提交必须与原集合一致，新增/遗漏/替换 Draft 都抛本异常，由引擎转为
    结构化 ``review_batch_draft_set_mismatch`` 回喂模型（不建/不改 Batch）。
    """

    code = "review_batch_draft_set_mismatch"

    def __init__(self, *, frozen: list[str], submitted: list[str]) -> None:
        self.frozen = frozen
        self.submitted = submitted
        super().__init__(
            f"submitted draft set {submitted} does not match the frozen review "
            f"batch draft set {frozen}; revise the original drafts or finish the run"
        )


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


async def release_run_drafts(db: AsyncSession, run_id: str, *, outcome: str = "idle") -> None:
    """释放某 Run 持有的全部 Draft working head，保留不可变 Revision（§5.7）。

    与 :meth:`ReviewerDriver.cancel_reviewing` 同一语义（已释放幂等跳过、他人
    持有抛 ``ArtifactBusy``），Draft 集合按 ``owner_run_id`` 自动发现——引擎的
    非发布出口（ask_user/complete/paused/cancelled/failed）与执行器/恢复循环
    的取消孤儿收口（I1）共用本函数，任何非发布出口都不得让 Artifact 永久
    ``artifact_busy``。
    """
    if outcome not in ("idle", "failed"):
        raise ValueError(f"invalid cancel outcome: {outcome!r}")
    service = ArtifactService(db)
    draft_ids = list(
        (
            await db.scalars(
                select(ArtifactDraft.id).where(ArtifactDraft.owner_run_id == run_id)
            )
        ).all()
    )
    for draft_id in draft_ids:
        draft = await db.get(ArtifactDraft, draft_id)
        if draft is None or draft.owner_run_id is None:
            continue  # 已释放，幂等
        if draft.owner_run_id != run_id:
            raise ArtifactBusy(
                draft.artifact_id,
                draft_id=draft.id,
                owner_run_id=draft.owner_run_id,
            )
        await service.release_draft(draft.id, outcome=outcome)


class ReviewerDriver:
    """Reviewer 生命周期驱动：批次创建 + 单 Item 复核 + 整批发布前复核。

    内部 Run 的创建模式与 AgentRun 一致（含 Attempt / Step / token 审计），
    但 Reviewer 永不调用工具，因此不会产生任何 ``agent_tool_calls`` 行。
    ``worker_id`` 是**必填**参数：它标识父 Run 租约持有者（引擎传入自己的
    worker id）——复核/发布流程中的状态一致性以租约为准；父 Run 的终态
    迁移（reviewing→completed/failed）由引擎经终态事务边界完成（H1），
    worker 不一致会在迁移时抛 ``run_lease_not_held``。
    """

    def __init__(
        self,
        db: AsyncSession,
        gateway: AgentModelGateway,
        *,
        worker_id: str,
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

        先整体校验全部 Draft 存在且归本 Run 所有（幻觉/他人 draft_id 抛
        ``LookupError``/``ArtifactBusy``），再写任何行——绝不留下半成品 Batch。
        首次创建后 Batch 的 Draft 集合与 completion_text 即冻结（§5.7）。
        """
        drafts: list[ArtifactDraft] = []
        for draft_id in draft_ids:
            draft = await self.db.get(ArtifactDraft, draft_id)
            if draft is None:
                raise LookupError(f"draft {draft_id!r} not found")
            if draft.owner_run_id != parent_run_id:
                raise ArtifactBusy(
                    draft.artifact_id,
                    draft_id=draft.id,
                    owner_run_id=draft.owner_run_id,
                )
            drafts.append(draft)
        batch = ArtifactReviewBatch(
            id=str(uuid4()),
            parent_run_id=parent_run_id,
            status="pending",
            completion_text=completion_text,
            created_at=_utcnow(),
        )
        self.db.add(batch)
        await self.db.flush()
        for draft in drafts:
            # working head 置为 reviewing（owner 已预校验是本 Run）。
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
        - reject → 整批 failed、全部 Draft/Artifact failed；父 Run 的终态迁移
          由引擎经终态事务边界完成（H1），不在本方法内。
        """
        now = _utcnow()
        batch = await self.db.get(ArtifactReviewBatch, item.batch_id)
        if batch is None:
            raise LookupError(f"review batch {item.batch_id!r} not found")
        # 防御：Item 必须属于传入的父 Run，防止审计/状态迁移错位。
        if batch.parent_run_id != parent_run.id:
            raise ReviewOwnershipError(
                f"review item {item.id!r} belongs to batch {batch.id!r} of run "
                f"{batch.parent_run_id!r}, not run {parent_run.id!r}"
            )
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
        try:
            runtime_snapshot = await RuntimeConfigService(self.db).snapshot_for_child_run(
                parent_run, profile_name="artifact_reviewer_v1"
            )
        except RuntimeConfigError:
            # Runtime snapshot tampering/missing configuration is a stable
            # fail-closed reviewer failure; do not create a child Run.
            raise
        review_run = self._new_review_run(parent_run, now, runtime_snapshot)
        self.db.add(review_run)
        await self.db.flush()
        review_attempt = await self._repo.begin_attempt(review_run.id)

        # 只读不可变 Revision + 解析 lineage + Schema + 限制，构造评审上下文。
        context = await self._build_context(parent_run, current_rev, user_question)
        review_run.prompt_snapshot_json = context

        # 瞬时生成失败（如推理模型在大上下文复核时输出不可解析）在适配器
        # 内部重生成之外再做有限整体重试：不占用复核次数额度（§12.3 的
        # 三次上限针对真实复核结论，不含模型输出失败）。step_sequence 逐次
        # 递增以维持 (run_id, sequence) 唯一。
        decision = None
        last_invalid: ModelPlanInvalidError | None = None
        for retry_index in range(3):
            try:
                decision = await self.gateway.decide(
                    run=review_run,
                    attempt_id=review_attempt.id,
                    profile=get_profile("artifact_reviewer_v1"),
                    messages=[
                        ChatMessage(role="user", content=json.dumps(context, ensure_ascii=False))
                    ],
                    thinking_sink=None,
                    step_sequence=1 + retry_index,
                    purpose="artifact_reviewer",
                    template_name="artifact_reviewer_v1",
                    decision_root=REVIEW_DECISION_ROOT,
                )
                break
            except ModelPlanInvalidError as exc:
                last_invalid = exc
                continue
        if decision is None:
            raise last_invalid  # type: ignore[misc]

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

    # -- 取消 / 系统失败回收 --------------------------------------------------------

    async def cancel_reviewing(
        self,
        *,
        run_id: str,
        draft_ids: tuple[str, ...] | list[str],
        outcome: str = "failed",
    ) -> None:
        """非发布出口释放仍属于 ``run_id`` 的 working head（§5.7）。

        取消、系统失败、ask_user/complete/paused/failed 出口都经本方法把该
        Run 持有的 Draft 置回 ``idle``/``failed`` 并释放 ``owner_run_id``，
        历史 Revision 永久保留；此后新 Run 可立即接管同一 Artifact，避免
        Artifact 永久 ``artifact_busy``。不属于本 Run 的 Draft 抛
        ``ArtifactBusy``（防误释放）；已释放（owner 为 None）的 Draft 幂等跳过。
        """
        if outcome not in ("idle", "failed"):
            raise ValueError(f"invalid cancel outcome: {outcome!r}")
        for draft_id in draft_ids:
            draft = await self.db.get(ArtifactDraft, draft_id)
            if draft is None:
                raise LookupError(f"draft {draft_id!r} not found")
            if draft.owner_run_id is None:
                continue  # 已释放，幂等
            if draft.owner_run_id != run_id:
                raise ArtifactBusy(
                    draft.artifact_id,
                    draft_id=draft.id,
                    owner_run_id=draft.owner_run_id,
                )
            await self._service.release_draft(draft.id, outcome=outcome)

    # -- 内部工具 -----------------------------------------------------------------

    def _new_review_run(
        self,
        parent_run: AgentRun,
        now: datetime,
        runtime_snapshot: RuntimeConfigSnapshot,
    ) -> AgentRun:
        return AgentRun(
            id=str(uuid4()),
            session_id=parent_run.session_id,
            user_id=parent_run.user_id,
            tenant_id=parent_run.tenant_id,
            parent_run_id=parent_run.id,
            run_kind="internal",
            visibility="internal",
            profile_name=get_profile("artifact_reviewer_v1").full_name,
            profile_version=get_profile("artifact_reviewer_v1").version,
            model=parent_run.model,
            runtime_backend=runtime_snapshot.runtime_backend,
            runtime_config_version_id=runtime_snapshot.config_version_id,
            runtime_config_snapshot_json=runtime_snapshot.model_dump(mode="json"),
            queued_at=now,
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
        """reject 收口：整批 failed、全部 Draft/Artifact failed。

        父 Run 的终态迁移不在此处——由引擎经终态事务边界
        （``AgentEventStream.settle_terminal``）与 run.failed 事件一体提交
        （H1：消除"已终态无事件"窗口）；本方法的清理随引擎随后的
        ``review.rejected`` 事件 append 一起提交。
        """
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


__all__ = [
    "MAX_REVIEW_ATTEMPTS",
    "REVIEW_DECISION_ROOT",
    "ReviewAttemptResult",
    "ReviewBatchDraftSetMismatch",
    "ReviewDecision",
    "ReviewIssue",
    "ReviewLimitExceeded",
    "ReviewOwnershipError",
    "ReviewerDriver",
    "release_run_drafts",
]
