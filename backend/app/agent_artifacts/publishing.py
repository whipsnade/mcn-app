"""确定性直接发布服务（直接发布改造 Task 3；设计 §10.2/§10.3）。

取代模型 Reviewer 的发布路径：

- ``ArtifactPublicationService.publish`` 逐 Draft 独立事务发布：每个 Draft
  单独校验（payload 强类型二次校验 + ``ArtifactLineageFreezer`` 冻结 lineage
  传递闭包）、单独提交，一个失败不回滚其他成功项；
- 同一 Draft Revision 幂等：``idempotency_key = publish:{draft_revision_id}``
  唯一约束兜底，重放返回已落库的 ``ArtifactPublishAttempt`` 结果，不生成重复
  Version；已发布 Version 永不更新；
- 校验快照（payload/lineage 两个阶段 + 扁平 errors）写入
  ``ArtifactPublishAttempt.validation_json``，发布成功时同一份快照随 Version
  ``validation_json`` 落库；新 Version ``review_json=None``，不写任何 Review 表；
- ``validation_failed`` 保留 Draft owner（仍 drafting）供模型修订后重发——修订
  产生新 Revision、新幂等键，原 Attempt 永久保留；
- ``abandon_draft``：只有 owner Run 能把未发布 Draft 置 failed，结构化原因记入
  ``ArtifactPublishAttempt(status="failed", error_code=reason_code)``
  （``validation_json`` 存 reason 明细），并追加 ``failed`` artifact 事件；
  不给 Draft 表临时增加错误列。

租约边界（§5.5 与 ``publish_batch`` 对齐）：``publish`` 入口复核调用方 worker
仍持有 Run 活跃租约，否则抛 ``run_lease_not_held``；逐项已提交的成功项不回滚。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_artifacts.lineage import (
    ArtifactLineageFreezer,
    LineageError,
    LineageOwner,
    evidence_scope_from_snapshot,
    validate_structured_claims,
)
from app.agent_artifacts.models import (
    AgentArtifact,
    AgentArtifactVersion,
    ArtifactDraft,
    ArtifactDraftRevision,
    ArtifactPublishAttempt,
)
from app.agent_artifacts.publication_core import validate_payload_for_publication
from app.agent_artifacts.service import ArtifactBusy, ArtifactService, _utcnow
from app.agent_runtime.models import AgentRun
from app.agent_runtime.repository import AgentRunRepository
from app.agent_runtime.state import InvalidRunTransition

# 逐项失败的结构化错误码（写入 PublishItemResult.errors / 工具回喂）。
DRAFT_NOT_FOUND = "draft_not_found"
# 归属失败复用 working head 并发词汇（ArtifactBusy.code 同为 artifact_busy）。
ARTIFACT_BUSY = "artifact_busy"
# 放弃已发布 Draft 的结构化拒绝（abandon_draft 工具映射为 error_type）。
DRAFT_ALREADY_PUBLISHED = "draft_already_published"


class DraftAlreadyPublished(Exception):
    """放弃一个当前 Revision 已发布的 Draft（结构化错误，非崩溃）。"""

    code = DRAFT_ALREADY_PUBLISHED


@dataclass(frozen=True)
class PublishItemResult:
    """单个 Draft 的发布结果（回喂模型的逐项结论）。"""

    draft_id: str
    status: Literal["published", "validation_failed", "failed"]
    artifact_id: str
    artifact_version_id: str | None
    version: int | None
    errors: tuple[dict[str, Any], ...] = ()


def _publish_idempotency_key(draft_revision_id: str) -> str:
    """同一 Draft Revision 的发布幂等键（唯一约束兜底，重放不重复发布）。"""
    return f"publish:{draft_revision_id}"


def _abandon_idempotency_key(draft_revision_id: str) -> str:
    """放弃操作的幂等键命名空间与发布隔离：放弃记录永不阻塞后续重发流程。"""
    return f"abandon:{draft_revision_id}"


def _reject_idempotency_key(run_id: str, draft_id: str) -> str:
    """引用失败（draft 不存在 / revision 缺失 / 他人持有）的拒绝记录幂等键。

    命名空间与 publish/abandon 隔离：拒绝记录不阻塞同一 Revision 后续真 owner
    发布；前缀 run_id 保证不同 Run 幻觉同一 draft_id 不撞唯一约束；draft_id 为
    模型幻觉输入（任意长度），键内哈希固定长度避免超 String(191) 列宽；同 Run
    重复幻觉同一 draft_id 幂等复用已有记录。
    """
    return f"reject:{run_id}:{_reject_draft_hash(draft_id)}"


def _reject_draft_hash(draft_id: str) -> str:
    return hashlib.sha256(draft_id.encode("utf-8")).hexdigest()


async def _record_rejection(
    db: AsyncSession,
    *,
    run_id: str,
    draft_id: str,
    code: str,
    message: str,
) -> PublishItemResult:
    """把引用失败持久化为 failed Attempt（Gate A 审查修复）。

    失败记录同样参与终态聚合（engine._publish_outcome_artifact_ids），Run
    不会在存在失败发布项时被错误标记为 completed。**拒绝记录不保存外部
    Artifact 身份**（``artifact_id``/``draft_revision_id`` 恒为 NULL，避免
    泄漏他人 Artifact 存在性），仅存模型自报的 ``draft_id`` 供终态聚合标识；
    已有同幂等键记录（同 Run 重复幻觉同一 draft_id）直接复用，不重复落行。
    """
    result = _failure_result(draft_id, code, message)
    existing = await db.scalar(
        select(ArtifactPublishAttempt).where(
            ArtifactPublishAttempt.idempotency_key == _reject_idempotency_key(run_id, draft_id)
        )
    )
    if existing is not None:
        return result
    now = _utcnow()
    db.add(
        ArtifactPublishAttempt(
            id=str(uuid4()),
            run_id=run_id,
            artifact_id=None,
            draft_revision_id=None,
            status="failed",
            idempotency_key=_reject_idempotency_key(run_id, draft_id),
            validation_json={
                "rejected_draft_id": draft_id,
                "reject_code": code,
                "reject_message": message,
            },
            error_code=code,
            created_at=now,
            completed_at=now,
        )
    )
    await db.commit()
    return result


def _failure_result(draft_id: str, code: str, message: str) -> PublishItemResult:
    return PublishItemResult(
        draft_id=draft_id,
        status="failed",
        artifact_id="",
        artifact_version_id=None,
        version=None,
        errors=({"code": code, "msg": message},),
    )


class ArtifactPublicationService:
    """逐 Draft 确定性校验并发布（无模型 Reviewer）。构造函数接收一个 AsyncSession。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self._artifacts = ArtifactService(db)
        self._freezer = ArtifactLineageFreezer(db)

    async def publish(
        self, *, run_id: str, draft_ids: tuple[str, ...], worker_id: str
    ) -> tuple[PublishItemResult, ...]:
        """逐 Draft 独立事务发布；逐项结果与去重后的 ``draft_ids`` 顺序一致。

        - 入口复核 ``worker_id`` 仍持有 ``run_id`` 的活跃租约（§5.5），否则抛
          ``InvalidRunTransition("run_lease_not_held")``；
        - ``PublishArtifacts`` schema 不拒绝重复 id：此处去重保序，同一 Draft
          在一次调用内只发布一次；
        - 每个 Draft 成功/校验失败都独立提交（per-artifact transaction），
          任一失败不回滚其他成功项。
        """
        run = await self.db.get(AgentRun, run_id)
        if run is None or not AgentRunRepository.owns_active_lease(run, worker_id):
            raise InvalidRunTransition("run_lease_not_held")
        results: list[PublishItemResult] = []
        for draft_id in dict.fromkeys(draft_ids):
            results.append(await self._publish_one(run_id=run_id, draft_id=draft_id))
        return tuple(results)

    async def _publish_one(self, *, run_id: str, draft_id: str) -> PublishItemResult:
        draft = await self.db.scalar(
            select(ArtifactDraft).where(ArtifactDraft.id == draft_id).with_for_update()
        )
        if draft is None:
            # 引用不存在的 Draft：持久化拒绝记录（参与终态聚合），不崩溃。
            return await _record_rejection(
                self.db,
                run_id=run_id,
                draft_id=draft_id,
                code=DRAFT_NOT_FOUND,
                message=f"draft {draft_id!r} not found",
            )
        revision = await self.db.scalar(
            select(ArtifactDraftRevision).where(
                ArtifactDraftRevision.draft_id == draft.id,
                ArtifactDraftRevision.revision == draft.current_revision,
            )
        )
        if revision is None:  # pragma: no cover - create_or_get 恒先写首个 Revision
            return await _record_rejection(
                self.db,
                run_id=run_id,
                draft_id=draft_id,
                code=DRAFT_NOT_FOUND,
                message=f"current revision {draft.current_revision} for draft {draft_id!r} not found",
            )

        # 幂等重放：同一 Revision 由**本 Run**已落终态 Attempt 时直接返回已存
        # 结果（不产生重复行）。他人 Run 的 Attempt（published/validation_failed/
        # validating/failed）一律不得复用——published 会泄漏外部 Artifact 身份、
        # validation_failed 会泄漏原错误快照——统一落入下方 not_found 拒绝
        # （Gate A 三审：幂等重放必须先过归属校验）。
        attempt = await self.db.scalar(
            select(ArtifactPublishAttempt).where(
                ArtifactPublishAttempt.idempotency_key == _publish_idempotency_key(revision.id)
            )
        )
        if attempt is not None and attempt.run_id == run_id:
            if attempt.status == "published":
                version = await self.db.get(AgentArtifactVersion, attempt.published_version_id)
                return PublishItemResult(
                    draft_id=draft_id,
                    status="published",
                    artifact_id=attempt.artifact_id,
                    artifact_version_id=attempt.published_version_id,
                    version=version.version if version is not None else None,
                )
            if attempt.status == "validation_failed":
                snapshot = attempt.validation_json or {}
                return PublishItemResult(
                    draft_id=draft_id,
                    status="validation_failed",
                    artifact_id=attempt.artifact_id,
                    artifact_version_id=None,
                    version=None,
                    errors=tuple(snapshot.get("errors") or ()),
                )

        if draft.owner_run_id != run_id:
            # 他人持有的 Draft：统一返回 not_found，不暴露 draft 存在性 / 归属
            # Run（Gate A 审查：不泄漏外部 Artifact 身份）；拒绝记录不保存外部
            # artifact_id，仅结构化回喂模型。
            return await _record_rejection(
                self.db,
                run_id=run_id,
                draft_id=draft_id,
                code=DRAFT_NOT_FOUND,
                message=f"draft {draft_id!r} not found",
            )

        if attempt is not None and attempt.run_id != run_id:
            # 防御：Attempt 属他人 Run 但 owner 已落本 Run（异常状态）——不得
            # 重置/复用他人记录，统一 not_found 拒绝。
            return await _record_rejection(
                self.db,
                run_id=run_id,
                draft_id=draft_id,
                code=DRAFT_NOT_FOUND,
                message=f"draft {draft_id!r} not found",
            )

        now = _utcnow()
        if attempt is None:
            attempt = ArtifactPublishAttempt(
                id=str(uuid4()),
                run_id=run_id,
                artifact_id=draft.artifact_id,
                draft_revision_id=revision.id,
                status="validating",
                idempotency_key=_publish_idempotency_key(revision.id),
                created_at=now,
            )
            self.db.add(attempt)
        else:
            # 既有 failed Attempt（如上次中途崩溃）：原地重试同一幂等键。
            attempt.status = "validating"
            attempt.error_code = None
            attempt.validation_json = None
            attempt.published_version_id = None
            attempt.completed_at = None
        await self.db.flush()

        artifact = await self.db.scalar(
            select(AgentArtifact)
            .where(AgentArtifact.id == draft.artifact_id)
            .with_for_update()
        )
        if artifact is None:  # pragma: no cover - FK 保证稳定身份存在
            attempt.status = "failed"
            attempt.error_code = DRAFT_NOT_FOUND
            attempt.completed_at = now
            await self.db.commit()
            return _failure_result(
                draft_id, DRAFT_NOT_FOUND, f"artifact {draft.artifact_id!r} not found"
            )

        # 发布门禁（§10.3）：payload 强类型二次校验 + lineage 传递闭包冻结。
        validated_payload, payload_errors = validate_payload_for_publication(
            module=artifact.module,
            schema_version=revision.schema_version,
            artifact_type=artifact.artifact_type,
            payload=revision.payload_json,
            enforce_kol_publication_validity=True,
        )
        lineage_snapshot: dict[str, Any] | None = None
        lineage_errors: list[dict[str, Any]] = []
        if not payload_errors:
            try:
                lineage_snapshot = await self._freezer.freeze(
                    payload=validated_payload,
                    refs=revision.evidence_refs_json,
                    owner=LineageOwner(
                        user_id=artifact.user_id,
                        session_id=artifact.session_id,
                        run_id=run_id,
                    ),
                )
            except LineageError as exc:
                lineage_errors = [{"stage": "lineage", "code": exc.code, "msg": exc.message}]

        candidate_version_id = str(uuid4())
        structured_errors: list[dict[str, Any]] = []
        structured_enabled = bool(
            validated_payload
            and (
                validated_payload.get("canonical_data")
                or validated_payload.get("field_lineage")
            )
        )
        if structured_enabled and not payload_errors and not lineage_errors:
            validated_payload, structured_errors = validate_payload_for_publication(
                module=artifact.module,
                schema_version=revision.schema_version,
                artifact_type=artifact.artifact_type,
                payload=validated_payload,
                evidence_scope=evidence_scope_from_snapshot(
                    lineage_snapshot,
                    owner=LineageOwner(
                        user_id=artifact.user_id,
                        session_id=artifact.session_id,
                        run_id=run_id,
                    ),
                ),
                artifact_version_id=candidate_version_id,
                enforce_kol_publication_validity=True,
                structured_validator=validate_structured_claims,
            )
        flat_errors: list[dict[str, Any]] = [
            {"stage": "payload", **error} for error in payload_errors
        ] + lineage_errors + structured_errors
        # JSON 归一（loc tuple → list 等）：快照、回喂与幂等重放共享同一形态。
        flat_errors = json.loads(json.dumps(flat_errors, ensure_ascii=False, default=str))
        payload_errors = [e for e in flat_errors if e.get("stage") == "payload"]
        lineage_errors = [e for e in flat_errors if e.get("stage") == "lineage"]
        structured_errors = [e for e in flat_errors if e.get("stage") == "structured_claims"]
        stages: dict[str, Any] = {
            "payload": {"valid": not payload_errors, "errors": payload_errors},
            "lineage": {"valid": not lineage_errors, "errors": lineage_errors},
        }
        if structured_enabled:
            if payload_errors or lineage_errors:
                stages["structured_claims"] = {
                    "status": "not_evaluated",
                    "valid": False,
                    "errors": [],
                }
            else:
                stages["structured_claims"] = {
                    "status": "evaluated",
                    "valid": not structured_errors,
                    "errors": structured_errors,
                }
        snapshot = {
            "module": artifact.module,
            "schema_version": revision.schema_version,
            "artifact_type": artifact.artifact_type,
            "valid": not flat_errors,
            "errors": flat_errors,
            "stages": stages,
        }

        if flat_errors:
            error_code = "artifact_payload_invalid"
            if lineage_errors:
                error_code = lineage_errors[0]["code"]
            elif structured_errors:
                error_code = structured_errors[0]["code"]
            attempt.status = "validation_failed"
            attempt.error_code = error_code
            attempt.validation_json = snapshot
            attempt.completed_at = now
            await self.db.commit()
            return PublishItemResult(
                draft_id=draft_id,
                status="validation_failed",
                artifact_id=artifact.id,
                artifact_version_id=None,
                version=None,
                errors=tuple(flat_errors),
            )

        version = await self._artifacts.finalize_published_version(
            artifact=artifact,
            draft=draft,
            revision=revision,
            validated_payload=validated_payload,
            lineage_snapshot=lineage_snapshot,
            source_run_id=run_id,
            review_json=None,
            validation_json=snapshot,
            artifact_version_id=candidate_version_id,
        )
        attempt.status = "published"
        attempt.error_code = None
        attempt.validation_json = snapshot
        attempt.published_version_id = version.id
        attempt.completed_at = now
        result = PublishItemResult(
            draft_id=draft_id,
            status="published",
            artifact_id=artifact.id,
            artifact_version_id=version.id,
            version=version.version,
        )
        await self.db.commit()
        return result

    async def abandon_draft(
        self, *, run_id: str, draft_id: str, reason_code: str, reason: str
    ) -> ArtifactPublishAttempt:
        """owner Run 放弃无法修复的未发布 Draft（设计 §4.4）。

        Draft 置 failed 并释放 owner；结构化原因记入
        ``ArtifactPublishAttempt(status="failed", error_code=reason_code)``
        （``validation_json`` 存 reason 明细），追加 ``failed`` artifact 事件。
        已发布（当前 Revision 有 published Attempt）抛 ``DraftAlreadyPublished``；
        非 owner 抛 ``ArtifactBusy``；Draft 不存在抛 ``KeyError``。
        """
        now = _utcnow()
        draft = await self.db.scalar(
            select(ArtifactDraft).where(ArtifactDraft.id == draft_id).with_for_update()
        )
        if draft is None:
            raise KeyError(f"draft {draft_id!r} not found")
        revision = await self.db.scalar(
            select(ArtifactDraftRevision).where(
                ArtifactDraftRevision.draft_id == draft.id,
                ArtifactDraftRevision.revision == draft.current_revision,
            )
        )
        if revision is None:  # pragma: no cover - create_or_get 恒先写首个 Revision
            raise KeyError(
                f"current revision {draft.current_revision} for draft {draft_id!r} not found"
            )
        published_attempt = await self.db.scalar(
            select(ArtifactPublishAttempt).where(
                ArtifactPublishAttempt.idempotency_key
                == _publish_idempotency_key(revision.id)
            )
        )
        if published_attempt is not None and published_attempt.status == "published":
            raise DraftAlreadyPublished(
                f"draft {draft_id!r} current revision already published as "
                f"version {published_attempt.published_version_id!r}"
            )
        if draft.owner_run_id != run_id:
            raise ArtifactBusy(
                draft.artifact_id,
                draft_id=draft.id,
                owner_run_id=draft.owner_run_id,
            )
        artifact = await self.db.get(AgentArtifact, draft.artifact_id)
        if artifact is None:  # pragma: no cover - FK 保证稳定身份存在
            raise KeyError(f"artifact {draft.artifact_id!r} not found")

        attempt = ArtifactPublishAttempt(
            id=str(uuid4()),
            run_id=run_id,
            artifact_id=draft.artifact_id,
            draft_revision_id=revision.id,
            status="failed",
            idempotency_key=_abandon_idempotency_key(revision.id),
            validation_json={
                "abandoned": True,
                "reason_code": reason_code,
                "reason": reason,
            },
            error_code=reason_code,
            created_at=now,
            completed_at=now,
        )
        self.db.add(attempt)
        await self._artifacts.release_draft(draft.id, outcome="failed")
        event = await self._artifacts.emit_event(
            session_id=draft.session_id,
            user_id=artifact.user_id,
            module=artifact.module,
            artifact_id=artifact.id,
            event_type="failed",
            draft_revision=revision.revision,
        )
        artifact.activity_sequence = event.sequence
        artifact.updated_at = now
        await self.db.commit()
        return attempt


__all__ = [
    "ARTIFACT_BUSY",
    "DRAFT_ALREADY_PUBLISHED",
    "DRAFT_NOT_FOUND",
    "ArtifactPublicationService",
    "DraftAlreadyPublished",
    "PublishItemResult",
]
