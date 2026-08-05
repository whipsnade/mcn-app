"""Artifact Draft 生命周期与未读水位服务（设计文档 §8.1 / Task 12；v3 加固 §5.6/§5.7）。

服务端职责：
1. 稳定 Artifact 身份：``(session_id, artifact_key)`` 唯一；模型只提供业务字段，
   服务端用 ``build_artifact_key`` 生成 key，模型不能直接指定数据库 key；
2. 强类型发布边界（§5.6）：create/update Draft 先过 ``ArtifactPayloadValidator``
   （固定组合 + business fields 非空 + payload 契约），落库标准化
   ``model_dump(mode="json")``，失败抛 ``ArtifactPayloadInvalid`` 不写任何行；
   发布事务内锁定 Revision 后二次校验（防旧 Draft/旁路绕过）；
3. 不可变 Draft Revision：每次更新先插入新 Revision（``(draft_id, revision)`` 唯一），
   再以乐观锁推进 ``artifact_drafts.current_revision``；旧 Revision 永久保留；
4. artifact_busy 并发：working head 只允许一个活动 Run 持有；他人抢占返回结构化
   ``ArtifactBusy``（``code == "artifact_busy"``），不覆盖、不静默丢写；
   owner 释放（publish/fail/全出口释放）或旧 owner 已非活动（paused/终态/消失）
   后新 Run 才能接管（§5.7）；
5. Session 级 artifact sequence：每次创建/更新 Draft 递增 ``artifact_events.sequence``，
   事件携带 ``draft_revision`` 与稳定 ``artifact_id``；
6. 未读水位：``agent_artifact_read_states`` 按 (user, session, module) 记录
   ``last_seen_sequence``，只前进到前端已渲染的 sequence（max(old, new)），
   绝不后退。

``publish_batch`` 是发布事务：锁定 Batch/Item/Draft/Artifact 后先做发布边界校验
（强类型二次校验 + ``ArtifactLineageFreezer`` 冻结 lineage 传递闭包写入
``lineage_snapshot_json``，``evidence_refs_json`` 原样保留模型直接引用），
全部通过才一次性插入全部不可变 Version。插入 Version、释放 working head、
追加 published 事件与更新稳定身份的收尾序列抽在 ``finalize_published_version``
（单 Draft helper），供旧 Review Batch 路径与新的确定性直接发布服务
（``agent_artifacts.publishing.ArtifactPublicationService``）共用。
已读水位自迁移 0028 起读写独立的 ``agent_artifact_read_states``
（session FK → agent_sessions）；遗留 ``artifact_read_states``
（``app.artifacts.models.ArtifactReadState``）保持不动，仅供旧应用版本回滚。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_artifacts.keys import build_artifact_key
from app.agent_artifacts.lineage import ArtifactLineageFreezer, LineageOwner
from app.agent_artifacts.models import (
    AgentArtifact,
    AgentArtifactReadState,
    AgentArtifactVersion,
    ArtifactDraft,
    ArtifactDraftRevision,
    ArtifactEvent,
    ArtifactReviewAttempt,
    ArtifactReviewBatch,
    ArtifactReviewItem,
)
from app.agent_artifacts.validation import (
    ArtifactPayloadInvalid as ArtifactPayloadInvalid,
    ArtifactPayloadValidator,
)
from app.agent_runtime.models import AgentMessage, AgentRun
from app.agent_runtime.repository import AgentRunRepository
from app.agent_runtime.state import InvalidRunTransition, RunStatus

# 已暂停/终态的 Draft owner 不再视为「活动」：不阻塞新 Run 接管 working head
# （与 kol_detail 的 _NON_ACTIVE_OWNER_STATUSES 模式对齐，§5.7）。
_NON_ACTIVE_OWNER_STATUSES = frozenset(
    {
        RunStatus.PAUSED,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    }
)


class PublishBlocked(Exception):
    """批量发布被阻止（设计 §12.3 / Task 13）。

    任一 Item 未在当前 Revision 上 approve、或 Batch 已终态/无 Item 时抛出；
    调用方不得忽略——整批回滚、不产生任何部分 Version。
    """


class ArtifactBusy(Exception):
    """working head 被另一个活动 Run 持有（设计 §8.1）。结构化错误，非崩溃。

    ``code == "artifact_busy"``，并携带 artifact_key / draft_id / owner_run_id，
    供上层分类展示，不得被当作普通异常静默吞掉。
    """

    code = "artifact_busy"

    def __init__(
        self,
        artifact_key: str,
        *,
        draft_id: str | None = None,
        owner_run_id: str | None = None,
    ) -> None:
        self.artifact_key = artifact_key
        self.draft_id = draft_id
        self.owner_run_id = owner_run_id
        super().__init__(
            f"artifact {artifact_key!r} is busy; working head owned by run {owner_run_id!r}"
        )


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _payload_hash(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ArtifactService:
    """Draft/Revision 生命周期 + 未读水位。构造函数接收一个 AsyncSession。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # -- Session 级 artifact sequence ------------------------------------------

    async def _next_session_sequence(self, session_id: str) -> int:
        current = await self.db.scalar(
            select(func.max(ArtifactEvent.sequence)).where(
                ArtifactEvent.session_id == session_id
            )
        )
        return (current or 0) + 1

    async def emit_event(
        self,
        *,
        session_id: str,
        user_id: str,
        module: str,
        artifact_id: str,
        event_type: str,
        draft_revision: int | None = None,
        artifact_version_id: str | None = None,
    ) -> ArtifactEvent:
        event = ArtifactEvent(
            session_id=session_id,
            user_id=user_id,
            sequence=await self._next_session_sequence(session_id),
            module=module,
            artifact_id=artifact_id,
            event_type=event_type,
            draft_revision=draft_revision,
            artifact_version_id=artifact_version_id,
            created_at=_utcnow(),
        )
        self.db.add(event)
        return event

    # -- Draft 生命周期 ----------------------------------------------------------

    async def create_or_get_draft(
        self,
        session_id: str,
        user_id: str,
        run_id: str,
        module: str,
        business_fields: dict[str, Any],
        schema_version: str,
        payload: dict[str, Any],
        evidence_refs: list[dict[str, Any]] | None = None,
        *,
        artifact_type: str,
        parent_artifact_id: str | None = None,
        parent_artifact_version_id: str | None = None,
    ) -> tuple[AgentArtifact, ArtifactDraft, ArtifactDraftRevision]:
        """构建 key → 锁定/创建稳定身份 → 锁定/认领 working head → 写入首个 Revision。

        - 稳定身份不存在时创建（``(session_id, artifact_key)`` 唯一约束兜底）；
        - 另一个活动 Run 持有 working head 时抛 ``ArtifactBusy``；旧 owner 已
          非活动（paused/终态/消失）时新 Run 直接接管（§5.7）；
        - ``parent_artifact_version_id`` 只写进 Draft Revision，不写稳定行；
        - payload 先过强类型校验（§5.6），落库为标准化 ``model_dump(mode="json")``，
          失败抛 ``ArtifactPayloadInvalid``、不落任何行。
        """
        normalized_payload = ArtifactPayloadValidator.validate_new_draft(
            module=module,
            schema_version=schema_version,
            artifact_type=artifact_type,
            business_fields=business_fields,
            payload=payload,
        )
        artifact_key = build_artifact_key(module, **business_fields)
        now = _utcnow()

        artifact = await self.db.scalar(
            select(AgentArtifact)
            .where(
                AgentArtifact.session_id == session_id,
                AgentArtifact.artifact_key == artifact_key,
            )
            .with_for_update()
        )
        created_new = artifact is None
        if artifact is None:
            artifact = AgentArtifact(
                session_id=session_id,
                user_id=user_id,
                module=module,
                artifact_type=artifact_type,
                parent_artifact_id=parent_artifact_id,
                artifact_key=artifact_key,
                status="draft",
                latest_version=0,
                activity_sequence=0,
                created_at=now,
                updated_at=now,
            )
            self.db.add(artifact)
            await self.db.flush()

        draft = await self.db.scalar(
            select(ArtifactDraft)
            .where(ArtifactDraft.artifact_id == artifact.id)
            .with_for_update()
        )
        if draft is None:
            draft = ArtifactDraft(
                artifact_id=artifact.id,
                session_id=session_id,
                owner_run_id=run_id,
                current_revision=0,
                status="drafting",
                review_count=0,
                revision_count=0,
                updated_at=now,
            )
            self.db.add(draft)
            await self.db.flush()
        elif (
            draft.owner_run_id is not None
            and draft.owner_run_id != run_id
            and await self._owner_is_active(draft.owner_run_id)
        ):
            raise ArtifactBusy(
                artifact_key,
                draft_id=draft.id,
                owner_run_id=draft.owner_run_id,
            )
        else:
            # 空闲/本人持有/旧 owner 已非活动（§5.7）：认领（切换 owner 到当前 Run）
            draft.owner_run_id = run_id
            draft.status = "drafting"
            draft.updated_at = now

        revision_no = draft.current_revision + 1
        revision = ArtifactDraftRevision(
            draft_id=draft.id,
            artifact_id=artifact.id,
            run_id=run_id,
            revision=revision_no,
            schema_version=schema_version,
            payload_json=normalized_payload,
            evidence_refs_json=evidence_refs,
            parent_artifact_version_id=parent_artifact_version_id,
            payload_hash=_payload_hash(normalized_payload),
            created_at=now,
        )
        self.db.add(revision)
        draft.current_revision = revision_no
        draft.revision_count += 1
        draft.updated_at = now

        event = await self.emit_event(
            session_id=session_id,
            user_id=user_id,
            module=module,
            artifact_id=artifact.id,
            event_type="draft_created" if created_new else "draft_updated",
            draft_revision=revision_no,
        )
        artifact.activity_sequence = event.sequence
        artifact.updated_at = now

        await self.db.flush()
        return artifact, draft, revision

    async def update_draft(
        self,
        run_id: str,
        draft_id: str,
        payload: dict[str, Any],
        evidence_refs: list[dict[str, Any]] | None = None,
    ) -> tuple[ArtifactDraft, ArtifactDraftRevision]:
        """乐观更新：插入不可变 Revision（current+1），推进 current_revision，写事件。

        只有 working head 当前 owner（run_id 匹配）才能更新；否则抛 ``ArtifactBusy``。
        """
        now = _utcnow()
        draft = await self.db.scalar(
            select(ArtifactDraft).where(ArtifactDraft.id == draft_id).with_for_update()
        )
        if draft is None:
            raise KeyError(f"draft {draft_id!r} not found")
        if draft.owner_run_id != run_id:
            raise ArtifactBusy(
                draft.artifact_id,
                draft_id=draft.id,
                owner_run_id=draft.owner_run_id,
            )

        artifact = await self.db.get(AgentArtifact, draft.artifact_id)
        if artifact is None:  # pragma: no cover - FK 保证稳定身份存在
            raise KeyError(f"artifact {draft.artifact_id!r} not found")

        current_revision = await self.db.scalar(
            select(ArtifactDraftRevision).where(
                ArtifactDraftRevision.draft_id == draft.id,
                ArtifactDraftRevision.revision == draft.current_revision,
            )
        )
        schema_version = (
            current_revision.schema_version if current_revision is not None else artifact.artifact_type
        )
        # 强类型校验（§5.6）：失败抛 ArtifactPayloadInvalid，不写新 Revision。
        normalized_payload = ArtifactPayloadValidator.validate_revision_payload(
            module=artifact.module,
            schema_version=schema_version,
            artifact_type=artifact.artifact_type,
            payload=payload,
        )

        revision_no = draft.current_revision + 1
        revision = ArtifactDraftRevision(
            draft_id=draft.id,
            artifact_id=artifact.id,
            run_id=run_id,
            revision=revision_no,
            schema_version=schema_version,
            payload_json=normalized_payload,
            evidence_refs_json=evidence_refs,
            parent_artifact_version_id=current_revision.parent_artifact_version_id
            if current_revision is not None
            else None,
            payload_hash=_payload_hash(normalized_payload),
            created_at=now,
        )
        self.db.add(revision)
        draft.current_revision = revision_no
        draft.revision_count += 1
        draft.updated_at = now

        event = await self.emit_event(
            session_id=draft.session_id,
            user_id=artifact.user_id,
            module=artifact.module,
            artifact_id=artifact.id,
            event_type="draft_updated",
            draft_revision=revision_no,
        )
        artifact.activity_sequence = event.sequence
        artifact.updated_at = now

        await self.db.flush()
        return draft, revision

    async def _owner_is_active(self, run_id: str) -> bool:
        """owner Run 是否仍活动（queued/running/reviewing/clarification）；缺失/
        paused/终态视为非活动，允许新 Run 接管 working head（§5.7）。"""
        owner = await self.db.get(AgentRun, run_id)
        if owner is None:
            return False
        return RunStatus(owner.status) not in _NON_ACTIVE_OWNER_STATUSES

    async def mark_draft_reviewing(self, run_id: str, draft_id: str) -> ArtifactDraft:
        """把 working head 置为 ``reviewing``（Task 13 发布事务的前置钩子）。"""
        draft = await self.db.scalar(
            select(ArtifactDraft).where(ArtifactDraft.id == draft_id).with_for_update()
        )
        if draft is None:
            raise KeyError(f"draft {draft_id!r} not found")
        if draft.owner_run_id != run_id:
            raise ArtifactBusy(
                draft.artifact_id,
                draft_id=draft.id,
                owner_run_id=draft.owner_run_id,
            )
        draft.status = "reviewing"
        draft.updated_at = _utcnow()
        await self.db.flush()
        return draft

    async def release_draft(
        self, draft_id: str, *, outcome: str = "idle"
    ) -> ArtifactDraft:
        """发布/失败后把 working head 复位并释放 owner（Task 13 调用）。

        ``outcome`` 取 ``idle``（发布完成）或 ``failed``（失败）。
        历史 Revision 永久保留，供审计与下一轮 Draft 继续递增。
        """
        if outcome not in ("idle", "failed"):
            raise ValueError(f"invalid release outcome: {outcome!r}")
        draft = await self.db.scalar(
            select(ArtifactDraft).where(ArtifactDraft.id == draft_id).with_for_update()
        )
        if draft is None:
            raise KeyError(f"draft {draft_id!r} not found")
        draft.status = outcome
        draft.owner_run_id = None
        draft.updated_at = _utcnow()
        await self.db.flush()
        return draft

    # -- 单 Draft 发布收尾（publish_batch 与直接发布服务共用）--------------------------

    async def finalize_published_version(
        self,
        *,
        artifact: AgentArtifact,
        draft: ArtifactDraft,
        revision: ArtifactDraftRevision,
        validated_payload: dict[str, Any],
        lineage_snapshot: dict[str, Any],
        source_run_id: str,
        review_json: dict[str, Any] | None,
        validation_json: dict[str, Any] | None = None,
    ) -> AgentArtifactVersion:
        """单 Draft 发布收尾：插入不可变 Version → 释放 working head → published 事件
        → 更新稳定身份（``latest_version``/``status``/activity 水位）。

        调用方负责前置校验（payload 强类型 + lineage 冻结）与外层事务边界；
        复制 Revision 的 ``parent_artifact_version_id``，``evidence_refs_json``
        原样保留模型直接引用，``lineage_snapshot_json`` 存冻结闭包。
        直接发布路径（无 Reviewer）传 ``review_json=None`` 与确定性
        ``validation_json`` 校验快照。
        """
        now = _utcnow()
        version = AgentArtifactVersion(
            id=str(uuid4()),
            artifact_id=artifact.id,
            version=artifact.latest_version + 1,
            source_run_id=source_run_id,
            source_draft_revision_id=revision.id,
            parent_artifact_version_id=revision.parent_artifact_version_id,
            schema_version=revision.schema_version,
            payload_json=validated_payload,
            evidence_refs_json=revision.evidence_refs_json,
            lineage_snapshot_json=lineage_snapshot,
            review_json=review_json,
            validation_json=validation_json,
            data_status=validated_payload["data_status"],
            created_at=now,
        )
        self.db.add(version)
        await self.db.flush()

        await self.release_draft(draft.id, outcome="idle")
        event = await self.emit_event(
            session_id=artifact.session_id,
            user_id=artifact.user_id,
            module=artifact.module,
            artifact_id=artifact.id,
            event_type="published",
            draft_revision=revision.revision,
            artifact_version_id=version.id,
        )
        artifact.latest_version = version.version
        artifact.status = "published"
        artifact.activity_sequence = event.sequence
        artifact.updated_at = now
        return version

    # -- 批量原子发布（Task 13）------------------------------------------------------

    async def publish_batch(
        self, review_batch_id: str, *, worker_id: str
    ) -> list[AgentArtifactVersion]:
        """原子发布一个 Review Batch（设计 §8.1 / §12.3）。

        ``worker_id`` 是**必填**参数：发布收尾时复核父 Run 租约仍属本 worker
        （§5.5），必须与父 Run 租约持有者一致（引擎传入自己的 worker id），
        否则抛 ``run_lease_not_held`` 连带整批发布回滚。父 Run 的终态迁移
        （reviewing→completed）不在本事务内——由终态事务边界
        （``AgentEventStream.settle_terminal``）与 run.completed 事件一体提交
        （H1：消除"已终态无事件"窗口）。

        单事务内：锁定 Batch + 全部 Item + Draft + Artifact；先校验所有 Item 都在
        **当前** Revision 上 approve（任一不满足即抛 ``PublishBlocked``、整批回滚，
        不产生任何部分 Version）；再对每个 Revision 做发布边界校验（§5.6：强类型
        payload 二次校验 + ``ArtifactLineageFreezer`` 冻结 lineage 传递闭包）；
        全部通过后才一次性插入全部 ``agent_artifact_versions``、更新
        ``agent_artifacts.latest_version/status``、把 Draft working head 置回
        ``idle`` 并释放 owner、追加 ``published`` 事件、写入 assistant 消息
        （``completion_text``），最后复核父 Run 租约。
        """
        now = _utcnow()
        batch = await self.db.scalar(
            select(ArtifactReviewBatch)
            .where(ArtifactReviewBatch.id == review_batch_id)
            .with_for_update()
        )
        if batch is None:
            raise KeyError(f"review batch {review_batch_id!r} not found")
        if batch.status in ("completed", "failed"):
            raise PublishBlocked(
                f"review batch {review_batch_id!r} already finalized: {batch.status}"
            )
        items = (
            await self.db.scalars(
                select(ArtifactReviewItem)
                .where(ArtifactReviewItem.batch_id == batch.id)
                .with_for_update()
            )
        ).all()
        if not items:
            raise PublishBlocked(f"review batch {review_batch_id!r} has no items")

        # 1) 全部校验通过前不写任何业务行（all-or-nothing）。
        plans: list[
            tuple[
                ArtifactReviewItem,
                ArtifactDraft,
                ArtifactDraftRevision,
                AgentArtifact,
                dict[str, Any],
                dict[str, Any],
            ]
        ] = []
        freezer = ArtifactLineageFreezer(self.db)
        for item in items:
            draft = await self.db.scalar(
                select(ArtifactDraft)
                .where(ArtifactDraft.artifact_id == item.artifact_id)
                .with_for_update()
            )
            if draft is None:
                raise PublishBlocked(f"draft for artifact {item.artifact_id!r} not found")
            current_rev = await self.db.scalar(
                select(ArtifactDraftRevision).where(
                    ArtifactDraftRevision.draft_id == draft.id,
                    ArtifactDraftRevision.revision == draft.current_revision,
                )
            )
            if current_rev is None:
                raise PublishBlocked(
                    f"revision {draft.current_revision} for draft {draft.id!r} not found"
                )
            if item.draft_revision_id != current_rev.id or item.status != "approved":
                raise PublishBlocked(
                    f"review item {item.id!r} is not approved on current revision "
                    f"(bound={item.draft_revision_id}, current={current_rev.id}, "
                    f"status={item.status!r})"
                )
            artifact = await self.db.scalar(
                select(AgentArtifact)
                .where(AgentArtifact.id == item.artifact_id)
                .with_for_update()
            )
            if artifact is None:
                raise PublishBlocked(f"artifact {item.artifact_id!r} not found")
            # 发布边界（§5.6）：锁定 Revision 后再次强类型校验，防旧 Draft/旁路
            # 写入绕过 create/update 校验（失败抛 ArtifactPayloadInvalid，整批回滚）。
            validated_payload = ArtifactPayloadValidator.validate_revision_payload(
                module=artifact.module,
                schema_version=current_rev.schema_version,
                artifact_type=artifact.artifact_type,
                payload=current_rev.payload_json,
            )
            # 冻结 lineage 传递闭包（菱形去重、跨层级展开），写入 Version 审计快照。
            lineage_snapshot = await freezer.freeze(
                payload=validated_payload,
                refs=current_rev.evidence_refs_json,
                owner=LineageOwner(
                    user_id=artifact.user_id,
                    session_id=artifact.session_id,
                    run_id=batch.parent_run_id,
                ),
            )
            plans.append(
                (item, draft, current_rev, artifact, validated_payload, lineage_snapshot)
            )

        # 2) 逐项落地不可变 Version 并收尾（单 Draft helper 与直接发布服务共用）：
        # 插入 Version → 释放 working head → 追加 published 事件 → 更新稳定身份。
        versions: list[AgentArtifactVersion] = []
        for item, draft, current_rev, artifact, validated_payload, lineage_snapshot in plans:
            version = await self.finalize_published_version(
                artifact=artifact,
                draft=draft,
                revision=current_rev,
                validated_payload=validated_payload,
                lineage_snapshot=lineage_snapshot,
                source_run_id=batch.parent_run_id,
                review_json=await self._review_json_for_item(item.id),
            )
            versions.append(version)
        await self.db.flush()

        # 3) Batch 完成 + 写 assistant 消息（completion_text 只在整批发布后落地）。
        batch.status = "completed"
        batch.completed_at = now
        await self._write_assistant_message(
            session_id=plans[0][3].session_id,
            run_id=batch.parent_run_id,
            content=batch.completion_text,
        )

        # 4) 发布前租约复核（§5.5/H1）：父 Run 的终态迁移（reviewing→completed）
        # 由终态事务边界（AgentEventStream.settle_terminal）与 run.completed
        # 事件一体提交；此处只复核租约仍属本 worker——丢失则抛
        # run_lease_not_held 连带整批发布回滚（不发布、不写终态，交还接管方）。
        parent_run = await self.db.get(AgentRun, batch.parent_run_id)
        if parent_run is None or not AgentRunRepository.owns_active_lease(
            parent_run, worker_id
        ):
            raise InvalidRunTransition("run_lease_not_held")
        await self.db.flush()
        return versions

    async def _review_json_for_item(self, review_item_id: str) -> dict[str, Any]:
        attempts = (
            await self.db.scalars(
                select(ArtifactReviewAttempt)
                .where(ArtifactReviewAttempt.review_item_id == review_item_id)
                .order_by(ArtifactReviewAttempt.attempt)
            )
        ).all()
        return {
            "decision": "approve",
            "attempts": len(attempts),
            "issues": [a.issues_json for a in attempts if a.issues_json] or None,
        }

    async def _write_assistant_message(
        self, *, session_id: str, run_id: str, content: str | None
    ) -> AgentMessage | None:
        if not content:
            return None
        sequence = await self.db.scalar(
            select(func.max(AgentMessage.sequence)).where(
                AgentMessage.session_id == session_id
            )
        )
        message = AgentMessage(
            id=str(uuid4()),
            session_id=session_id,
            run_id=run_id,
            role="assistant",
            content=content,
            metadata_json=None,
            sequence=(sequence or 0) + 1,
            created_at=_utcnow(),
        )
        self.db.add(message)
        return message

    # -- 未读水位 ------------------------------------------------------------------

    async def get_unread(self, user_id: str, session_id: str, module: str) -> bool:
        """模块最新 ``artifact_events.sequence > last_seen_sequence`` 即未读。"""
        latest = await self.db.scalar(
            select(func.max(ArtifactEvent.sequence)).where(
                ArtifactEvent.session_id == session_id,
                ArtifactEvent.module == module,
            )
        )
        if latest is None:
            return False
        table = AgentArtifactReadState.__table__
        row = await self.db.execute(
            select(table.c.last_seen_sequence).where(
                table.c.user_id == user_id,
                table.c.session_id == session_id,
                table.c.module == module,
            )
        )
        last_seen = row.scalar() or 0
        return latest > last_seen

    async def advance_read_state(
        self, user_id: str, session_id: str, module: str, sequence: int
    ) -> int:
        """水位只前进：``new = max(old, sequence)``，绝不后退。返回新的水位。

        读写独立的 ``agent_artifact_read_states``（迁移 0028 起，session FK 指向
        ``agent_sessions``，不再需要遗留 ``sessions`` 行）。并发首插竞争（另一事务
        已插入同一行）时捕获 IntegrityError 后重读并更新。
        """
        now = _utcnow()
        table = AgentArtifactReadState.__table__

        async def _load() -> Any:
            row = await self.db.execute(
                select(table.c.id, table.c.last_seen_sequence)
                .where(
                    and_(
                        table.c.user_id == user_id,
                        table.c.session_id == session_id,
                        table.c.module == module,
                    )
                )
                .with_for_update()
            )
            return row.first()

        existing = await _load()
        if existing is not None:
            new_sequence = max(existing.last_seen_sequence, sequence)
            if new_sequence != existing.last_seen_sequence:
                await self.db.execute(
                    table.update()
                    .where(table.c.id == existing.id)
                    .values(last_seen_sequence=new_sequence, updated_at=now)
                )
            return new_sequence

        try:
            await self.db.execute(
                table.insert().values(
                    id=str(uuid4()),
                    user_id=user_id,
                    session_id=session_id,
                    module=module,
                    last_seen_sequence=sequence,
                    updated_at=now,
                )
            )
        except IntegrityError:
            # 并发首插竞争：另一事务先插入了同一行 → 重读并更新，而不是把
            # DuplicateKey 抛给上层。
            existing = await _load()
            if existing is None:  # pragma: no cover - 竞态窗口内行必然已存在
                raise
            new_sequence = max(existing.last_seen_sequence, sequence)
            await self.db.execute(
                table.update()
                .where(table.c.id == existing.id)
                .values(last_seen_sequence=new_sequence, updated_at=now)
            )
            return new_sequence
        return sequence
