"""统一 Session Agent Engine（设计文档 §四 / §4.1 / §七 / §11.3 / Task 14）。

``AgentEngine.run`` 是模型主导的统一决策循环：反复 build context → model
decide → 分发四种动作（ask_user / call_tool / submit_review / complete），
并施加 Task 3 的 Attempt 保护（30 分钟 / 50 决策）、钱包规则、取消信号与
结构化校验错误。

**引擎是业务无关的**：不维护品牌/活动/KOL 阶段清单、固定工具顺序或
GoalPolicy；模型决定一切业务流程。代码只保留能力边界、状态、计费、证据、
校验与审计。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_artifacts.lineage import (
    DbLineageLoader,
    LineageError,
    LineageOwner,
    validate_and_freeze_lineage,
)
from app.agent_artifacts.models import (
    ArtifactDraft,
    ArtifactDraftRevision,
    ArtifactReviewAttempt,
    ArtifactReviewBatch,
    ArtifactReviewItem,
)
from app.agent_artifacts.service import ArtifactService
from app.agent_runtime.context import SessionContextBuilder
from app.agent_runtime.events import AgentEventStream
from app.agent_runtime.models import (
    AgentMessage,
    AgentRun,
    AgentRunAttempt,
    AgentStep,
    MemoryEntry,
)
from app.agent_runtime.profiles import AgentProfile
from app.agent_runtime.repository import (
    ATTEMPT_MAX_DECISIONS,
    ATTEMPT_MAX_SECONDS,
    AgentRunRepository,
    utc_now,
)
from app.agent_runtime.reviewer import ReviewerDriver
from app.agent_runtime.schemas import FOUR_ACTIONS
from app.agent_runtime.state import InvalidRunTransition, RunStatus
from app.agent_runtime.tools.contracts import ToolResult
from app.agent_runtime.tools.mcp import DEFINITELY_NOT_SENT
from app.agent_runtime.tools.registry import ToolRegistry, UnknownToolError
from app.billing.service import InsufficientPointsError
from app.model.contracts import ChatMessage

logger = logging.getLogger(__name__)

# 连续非法模型输出的安全阈值：达到后 Run 直接 failed（Task 14）。
MAX_INVALID_ACTIONS = 3

# call_tool 结果状态 → SSE 事件类型（§15.3）。
_TOOL_EVENT_BY_STATUS = {
    "success": "tool.succeeded",
    "failed": "tool.failed",
    "unknown": "tool.unknown",
}


@dataclass(frozen=True)
class RunOutcome:
    """一次 ``AgentEngine.run`` 的执行结果摘要。"""

    run_id: str
    status: RunStatus
    decision_count: int
    assistant_message_id: str | None = None


class AgentEngine:
    """模型主导的统一 Agent 决策循环。

    ``gateway`` 是 Task 6 的模型决策入口（测试注入脚本化动作的 fake）；
    ``reviewer`` 是 Task 13 的 Reviewer 驱动；``events`` 是 Task 4 事件流。
    ``worker_id`` 必须与父 Run 租约持有者一致，所有状态迁移才合法。
    """

    def __init__(
        self,
        db: AsyncSession,
        *,
        gateway: Any,
        registry: ToolRegistry,
        events: AgentEventStream,
        reviewer: ReviewerDriver,
        worker_id: str,
        repo: AgentRunRepository | None = None,
        context_builder: SessionContextBuilder | None = None,
        channel_permissions: Iterable[str] = (),
        lease_seconds: int = 300,
    ) -> None:
        self._db = db
        self._gateway = gateway
        self._registry = registry
        self._events = events
        self._reviewer = reviewer
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._repo = repo or AgentRunRepository(db)
        self._service = ArtifactService(db)
        self._context_builder = context_builder or SessionContextBuilder(db, registry)
        self._channel_permissions = tuple(channel_permissions or ())

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #

    async def run(
        self,
        *,
        run: AgentRun,
        attempt_id: str,
        profile: AgentProfile,
        messages: list[ChatMessage],
        thinking_sink: Any = None,
    ) -> RunOutcome:
        """执行一次用户 Run：直到终态 / clarification / paused / cancelled。"""
        conversation = list(messages)
        user_question = self._current_user_question(conversation)
        next_sequence = await self._next_step_sequence(run.id)
        await self._emit_run_started(run, attempt_id)
        consecutive_invalid = 0
        assistant_message_id: str | None = None

        while await self._is_running(run):
            # 续租：长 Attempt（最长 30 分钟）期间租约不能过期，否则 pause 静默
            # 失效、终态迁移抛 run_lease_not_held。续租失败视为不可恢复系统错误。
            if not await self._renew_lease(run):
                await self._fail_run(run)
                break
            if await self._guard_attempt_limits(run, attempt_id):
                await self._events.append(
                    run.id, run.user_id, "run.paused", {"attempt_id": attempt_id}
                )
                break
            if await self._cancel_requested(run):
                await self._repo.cancel(run.id, run.user_id)
                await self._events.append(run.id, run.user_id, "run.cancelled", {})
                break

            context = await self._build_context(run, profile, conversation, user_question)
            try:
                action = await self._gateway.decide(
                    run=run,
                    attempt_id=attempt_id,
                    profile=profile,
                    messages=context,
                    thinking_sink=thinking_sink,
                    step_sequence=next_sequence,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("model decision failed; failing run %s", run.id)
                await self._fail_run(run)
                break
            next_sequence += 1
            await self._count_decision(run, attempt_id)

            if not self._is_known_action(action):
                consecutive_invalid += 1
                if consecutive_invalid >= MAX_INVALID_ACTIONS:
                    await self._fail_run(run)
                    break
                self._feed_validation_error(conversation, action)
                continue
            consecutive_invalid = 0
            conversation.append(self._action_message(action))

            if action.action == "ask_user":
                message = await self._handle_ask_user(run, action)
                assistant_message_id = message.id
                break
            if action.action == "call_tool":
                call_result = await self._handle_call_tool(
                    run=run,
                    attempt_id=attempt_id,
                    profile=profile,
                    action=action,
                    conversation=conversation,
                    step_sequence=next_sequence,
                )
                next_sequence += 1
                if call_result == "cancelled":
                    # decide→dispatch 间隙收到取消：不发起新调用，直接收口 cancelled
                    await self._repo.cancel(run.id, run.user_id)
                    await self._events.append(run.id, run.user_id, "run.cancelled", {})
                    break
                continue
            if action.action == "submit_review":
                settled, published_message_id = await self._handle_submit_review(
                    run=run,
                    action=action,
                    conversation=conversation,
                    user_question=user_question,
                )
                if settled == "approved":
                    assistant_message_id = published_message_id or assistant_message_id
                    break
                if settled == "rejected":
                    break
                continue  # revise / lineage 错误 → 模型修正后继续
            if action.action == "complete":
                message = await self._handle_complete(run, action)
                assistant_message_id = message.id
                break

        final = await self._db.get(AgentRun, run.id)
        final_status = (
            RunStatus(final.status) if final is not None else RunStatus.FAILED
        )
        return RunOutcome(
            run_id=run.id,
            status=final_status,
            decision_count=run.decision_count,
            assistant_message_id=assistant_message_id,
        )

    # ------------------------------------------------------------------ #
    # 四种动作
    # ------------------------------------------------------------------ #

    async def _handle_ask_user(self, run: AgentRun, action: Any) -> AgentMessage:
        """ask_user：写 assistant 澄清消息 + pending Memory，Run 以 clarification 收尾。"""
        metadata = {
            "type": "clarification",
            "question": action.question,
            "options": list(action.options) if action.options is not None else None,
        }
        message = await self._append_message(
            session_id=run.session_id,
            run_id=run.id,
            role="assistant",
            content=action.question,
            metadata=metadata,
        )
        self._db.add(
            MemoryEntry(
                id=str(uuid4()),
                session_id=run.session_id,
                source_run_id=run.id,
                memory_type="pending_question",
                content_json={
                    "question": action.question,
                    "options": action.options,
                },
                created_at=utc_now(),
            )
        )
        await self._repo.transition(
            run.id, RunStatus.CLARIFICATION_REQUESTED, worker_id=self._worker_id
        )
        await self._events.append(
            run.id, run.user_id, "message.completed", {"type": "clarification"}
        )
        return message

    async def _handle_call_tool(
        self,
        *,
        run: AgentRun,
        attempt_id: str,
        profile: AgentProfile,
        action: Any,
        conversation: list[ChatMessage],
        step_sequence: int,
    ) -> str:
        """call_tool：可见性校验 → 外发前持久化 Step → 执行 → 结果回喂消息列表。

        ``InsufficientPointsError`` 作为结构化工具错误回喂模型（§11.3），
        不崩溃；已 settled 调用正常结算。返回 ``"cancelled"``（decide→dispatch
        间隙收到取消，未发起调用）或 ``"ok"``。
        """
        visible = await self._registry.visible_tools(
            profile, channel_permissions=self._channel_permissions
        )
        if not any(entry.internal_name == action.internal_tool_name for entry in visible):
            self._feed_tool_result(
                conversation,
                ToolResult(
                    status="failed",
                    safe_summary=f"tool not allowed for this profile: "
                    f"{action.internal_tool_name}",
                    error_type="unknown_tool",
                ),
            )
            return "ok"

        step = AgentStep(
            id=str(uuid4()),
            run_id=run.id,
            attempt_id=attempt_id,
            sequence=step_sequence,
            step_type="tool_call",
            input_json={
                "internal_tool_name": action.internal_tool_name,
                "arguments": action.arguments,
            },
            status="running",
            visibility=run.visibility,
            created_at=utc_now(),
        )
        self._db.add(step)
        await self._db.flush()
        await self._events.append(
            run.id,
            run.user_id,
            "tool.started",
            {"internal_tool_name": action.internal_tool_name},
        )

        # §11.3：decide（长模型调用）期间用户取消 → 外发前再核对一次，
        # 已请求取消则绝不发起新调用。
        if await self._cancel_requested(run):
            step.status = "failed"
            step.output_json = {
                "internal_tool_name": action.internal_tool_name,
                "status": "failed",
                "error_type": "cancelled_not_sent",
            }
            await self._db.flush()
            await self._events.append(
                run.id,
                run.user_id,
                "tool.failed",
                {
                    "internal_tool_name": action.internal_tool_name,
                    "status": "failed",
                    "error_type": "cancelled_not_sent",
                },
            )
            return "cancelled"

        try:
            result = await self._registry.execute(
                internal_name=action.internal_tool_name,
                arguments=action.arguments,
                user_id=run.user_id,
                session_id=run.session_id,
                run_id=run.id,
                profile=profile,
                channel_permissions=self._channel_permissions,
                step_id=step.id,
            )
        except InsufficientPointsError:
            result = ToolResult(
                status="failed",
                safe_summary="insufficient points for MCP call",
                error_type=DEFINITELY_NOT_SENT,
            )
        except UnknownToolError:
            result = ToolResult(
                status="failed",
                safe_summary=f"unknown tool: {action.internal_tool_name}",
                error_type="unknown_tool",
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "tool %s failed unexpectedly", action.internal_tool_name
            )
            result = ToolResult(
                status="failed",
                safe_summary="tool execution failed",
                error_type="internal_error",
            )

        step.status = "completed" if result.status == "success" else "failed"
        step.output_json = result.model_dump()
        await self._db.flush()
        await self._events.append(
            run.id,
            run.user_id,
            _TOOL_EVENT_BY_STATUS[result.status],
            {
                "internal_tool_name": action.internal_tool_name,
                "status": result.status,
                "error_type": result.error_type,
            },
        )
        self._feed_tool_result(conversation, result)
        return "ok"

    async def _handle_submit_review(
        self,
        *,
        run: AgentRun,
        action: Any,
        conversation: list[ChatMessage],
        user_question: str,
    ) -> tuple[str, str | None]:
        """submit_review：lineage 校验 → 建/复用 Batch → Reviewer 收口。

        返回 ``(settled, assistant_message_id)``：``settled`` 取
        ``approved`` / ``rejected``（终态）或 ``revise``（继续循环）；
        approve 时附带已发布消息 id（供 RunOutcome 回传）。
        """
        for draft_id in action.artifact_draft_ids:
            error = await self._validate_draft_lineage(run, draft_id)
            if error is not None:
                conversation.append(
                    ChatMessage(
                        role="user",
                        content=json.dumps(
                            {
                                "error_type": "lineage_error",
                                "message": error,
                                "draft_id": draft_id,
                            },
                            ensure_ascii=False,
                        ),
                    )
                )
                return "revise", None

        batch = await self._get_or_create_batch(run, action)
        await self._repo.transition(
            run.id, RunStatus.REVIEWING, worker_id=self._worker_id
        )
        await self._events.append(
            run.id,
            run.user_id,
            "review.started",
            {"review_batch_id": batch.id, "artifact_ids": list(action.artifact_draft_ids)},
        )

        try:
            results = await self._reviewer.review_pending(
                parent_run=run, batch=batch, user_question=user_question
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("reviewer failed for run %s; failing run", run.id)
            await self._abort_review(run, action)
            return "rejected", None

        decisions = {result.decision for result in results}
        if "reject" in decisions:
            await self._events.append(
                run.id,
                run.user_id,
                "review.rejected",
                {"review_batch_id": batch.id},
            )
            return "rejected", None
        if any(result.decision == "revise" for result in results):
            await self._events.append(
                run.id,
                run.user_id,
                "review.revision_requested",
                {"review_batch_id": batch.id},
            )
            await self._feed_review_issues(run, batch, conversation)
            await self._repo.transition(
                run.id, RunStatus.RUNNING, worker_id=self._worker_id
            )
            return "revise", None

        await self._events.append(
            run.id, run.user_id, "review.approved", {"review_batch_id": batch.id}
        )
        try:
            await self._service.publish_batch(batch.id, worker_id=self._worker_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("publish_batch failed for run %s; failing run", run.id)
            await self._abort_review(run, action)
            return "rejected", None
        await self._events.append(
            run.id, run.user_id, "run.completed", {"outcome": "completed"}
        )
        await self._events.append(
            run.id, run.user_id, "message.completed", {"type": "completion"}
        )
        published = await self._latest_assistant_message(run.id)
        return "approved", (published.id if published is not None else None)

    async def _handle_complete(self, run: AgentRun, action: Any) -> AgentMessage:
        """complete（无正式产物）：写 assistant 消息，Run → completed。"""
        metadata = {"type": "completion", "suggestions": action.suggestions}
        message = await self._append_message(
            session_id=run.session_id,
            run_id=run.id,
            role="assistant",
            content=action.text,
            metadata=metadata,
        )
        await self._repo.transition(
            run.id, RunStatus.COMPLETED, worker_id=self._worker_id
        )
        await self._events.append(
            run.id, run.user_id, "run.completed", {"outcome": "completed"}
        )
        await self._events.append(
            run.id, run.user_id, "message.completed", {"type": "completion"}
        )
        return message

    # ------------------------------------------------------------------ #
    # 循环守卫
    # ------------------------------------------------------------------ #

    async def _is_running(self, run: AgentRun) -> bool:
        fresh = await self._repo.lock_run(run.id)
        return fresh.status == RunStatus.RUNNING

    async def _cancel_requested(self, run: AgentRun) -> bool:
        fresh = await self._repo.lock_run(run.id)
        return bool(fresh.cancel_requested)

    async def _guard_attempt_limits(self, run: AgentRun, attempt_id: str) -> bool:
        """Attempt 保护（§七）：50 决策 / 30 分钟达阈值 → paused，返回 True。"""
        attempt = await self._db.get(AgentRunAttempt, attempt_id)
        if attempt is None:
            return False
        elapsed = (utc_now() - attempt.started_at).total_seconds()
        if (
            attempt.decision_count >= ATTEMPT_MAX_DECISIONS
            or elapsed >= ATTEMPT_MAX_SECONDS
        ):
            return await self._repo.pause(run.id, self._worker_id)
        return False

    async def _fail_run(self, run: AgentRun) -> None:
        """把 Run 收口为 failed；租约已丢失（过期/被接管）时回退到系统级 force_fail。

        正常路径持有租约经 ``transition`` 迁移；续租失败导致的系统错误无法持有
        租约，此时用 ``force_fail`` 干净收口，避免卡在 running + open attempt。
        """
        try:
            await self._repo.transition(
                run.id, RunStatus.FAILED, worker_id=self._worker_id
            )
            failed = True
        except InvalidRunTransition as exc:
            if "run_lease_not_held" not in str(exc):
                # 已是终态等：幂等跳过，不重复发失败事件。
                return
            failed = await self._repo.force_fail(run.id, error_code="run_lease_lost")
        if failed:
            await self._events.append(run.id, run.user_id, "run.failed", {"outcome": "failed"})

    async def _renew_lease(self, run: AgentRun) -> bool:
        try:
            return await self._repo.renew_lease(
                run.id, self._worker_id, self._lease_seconds
            )
        except Exception:
            logger.exception("lease renewal failed for run %s", run.id)
            return False

    async def _abort_review(self, run: AgentRun, action: Any) -> None:
        """Reviewer/发布异常时的收口：先释放 working head，再让 Run 失败。"""
        try:
            await self._reviewer.cancel_reviewing(
                run_id=run.id,
                draft_ids=action.artifact_draft_ids,
                outcome="failed",
            )
        except Exception:
            logger.exception("cancel_reviewing failed for run %s", run.id)
        await self._fail_run(run)

    async def _count_decision(self, run: AgentRun, attempt_id: str) -> None:
        run.decision_count += 1
        attempt = await self._db.get(AgentRunAttempt, attempt_id)
        if attempt is not None:
            attempt.decision_count += 1
        await self._db.flush()

    # ------------------------------------------------------------------ #
    # 上下文 / 消息
    # ------------------------------------------------------------------ #

    async def _build_context(
        self,
        run: AgentRun,
        profile: AgentProfile,
        conversation: list[ChatMessage],
        user_question: str,
    ) -> list[ChatMessage]:
        return await self._context_builder.build(
            run=run,
            profile=profile,
            conversation=conversation,
            current_user_message=user_question,
            channel_permissions=self._channel_permissions,
        )

    @staticmethod
    def _current_user_question(conversation: list[ChatMessage]) -> str:
        for message in reversed(conversation):
            if message.role == "user":
                return message.content
        return ""

    @staticmethod
    def _is_known_action(action: Any) -> bool:
        return bool(getattr(action, "action", None) in FOUR_ACTIONS)

    @staticmethod
    def _action_message(action: Any) -> ChatMessage:
        if isinstance(action, BaseModel):
            return ChatMessage(role="assistant", content=action.model_dump_json())
        return ChatMessage(
            role="assistant",
            content=json.dumps(vars(action), ensure_ascii=False, default=str),
        )

    def _feed_validation_error(self, conversation: list[ChatMessage], action: Any) -> None:
        conversation.append(self._action_message(action))
        conversation.append(
            ChatMessage(
                role="user",
                content=json.dumps(
                    {
                        "error_type": "validation_error",
                        "message": "invalid or unknown agent action: "
                        f"{getattr(action, 'action', None)!r}",
                    },
                    ensure_ascii=False,
                ),
            )
        )

    def _feed_tool_result(self, conversation: list[ChatMessage], result: ToolResult) -> None:
        conversation.append(
            ChatMessage(
                role="user",
                content=json.dumps(
                    {
                        "tool_result": {
                            "status": result.status,
                            "summary": result.safe_summary,
                            "evidence_id": result.evidence_id,
                            "cursor": result.cursor,
                            "truncated": result.truncated,
                            "error_type": result.error_type,
                        }
                    },
                    ensure_ascii=False,
                ),
            )
        )

    async def _feed_review_issues(
        self, run: AgentRun, batch: ArtifactReviewBatch, conversation: list[ChatMessage]
    ) -> None:
        """把最新一次 revise 的结构化问题回喂模型（§12.3）。"""
        items = (
            await self._db.scalars(
                select(ArtifactReviewItem).where(ArtifactReviewItem.batch_id == batch.id)
            )
        ).all()
        for item in items:
            latest = await self._db.scalar(
                select(ArtifactReviewAttempt)
                .where(
                    ArtifactReviewAttempt.review_item_id == item.id,
                    ArtifactReviewAttempt.decision == "revise",
                )
                .order_by(ArtifactReviewAttempt.attempt.desc())
            )
            if latest is None:
                continue
            conversation.append(
                ChatMessage(
                    role="user",
                    content=json.dumps(
                        {
                            "review_revision_requested": {
                                "artifact_id": item.artifact_id,
                                "issues": latest.issues_json or [],
                            }
                        },
                        ensure_ascii=False,
                    ),
                )
            )

    async def _append_message(
        self,
        *,
        session_id: str,
        run_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> AgentMessage:
        sequence = await self._db.scalar(
            select(func.max(AgentMessage.sequence)).where(
                AgentMessage.session_id == session_id
            )
        )
        message = AgentMessage(
            id=str(uuid4()),
            session_id=session_id,
            run_id=run_id,
            role=role,
            content=content,
            metadata_json=metadata,
            sequence=(sequence or 0) + 1,
            created_at=utc_now(),
        )
        self._db.add(message)
        await self._db.flush()
        return message

    async def _latest_assistant_message(self, run_id: str) -> AgentMessage | None:
        return await self._db.scalar(
            select(AgentMessage)
            .where(AgentMessage.run_id == run_id, AgentMessage.role == "assistant")
            .order_by(AgentMessage.sequence.desc())
            .limit(1)
        )

    # ------------------------------------------------------------------ #
    # 提交校验 / 批次
    # ------------------------------------------------------------------ #

    async def _validate_draft_lineage(self, run: AgentRun, draft_id: str) -> str | None:
        """提交 Reviewer 前校验 Draft 的字段级 lineage（§10.4 / Task 11）。

        失败返回结构化错误消息（回喂模型），成功返回 None。
        """
        draft = await self._db.get(ArtifactDraft, draft_id)
        if draft is None:
            return "draft not found"
        revision = await self._db.scalar(
            select(ArtifactDraftRevision).where(
                ArtifactDraftRevision.draft_id == draft.id,
                ArtifactDraftRevision.revision == draft.current_revision,
            )
        )
        if revision is None:
            return "draft revision not found"
        try:
            await validate_and_freeze_lineage(
                payload=revision.payload_json or {},
                refs=revision.evidence_refs_json or [],
                owner=LineageOwner(
                    user_id=run.user_id, session_id=run.session_id, run_id=run.id
                ),
                loader=DbLineageLoader(self._db),
            )
        except LineageError as exc:
            return f"lineage validation failed: {exc.code}: {exc.message}"
        except (ValidationError, ValueError) as exc:
            return f"lineage validation failed: {exc}"
        return None

    async def _get_or_create_batch(
        self, run: AgentRun, action: Any
    ) -> ArtifactReviewBatch:
        """一个用户 Run 最多一条 Review Batch（§8.1）：存在则复用。"""
        existing = await self._db.scalar(
            select(ArtifactReviewBatch).where(
                ArtifactReviewBatch.parent_run_id == run.id
            )
        )
        if existing is not None:
            return existing
        return await self._reviewer.create_batch(
            parent_run_id=run.id,
            draft_ids=action.artifact_draft_ids,
            completion_text=action.completion_text,
        )

    # ------------------------------------------------------------------ #
    # 审计辅助
    # ------------------------------------------------------------------ #

    async def _emit_run_started(self, run: AgentRun, attempt_id: str) -> None:
        attempt = await self._db.get(AgentRunAttempt, attempt_id)
        if attempt is not None and attempt.attempt > 1:
            await self._events.append(
                run.id,
                run.user_id,
                "run.resumed",
                {"attempt": attempt.attempt},
            )
            return
        await self._events.append(
            run.id, run.user_id, "run.started", {"run_kind": run.run_kind}
        )

    async def _next_step_sequence(self, run_id: str) -> int:
        current = await self._db.scalar(
            select(func.max(AgentStep.sequence)).where(AgentStep.run_id == run_id)
        )
        return (current or 0) + 1


__all__ = ["AgentEngine", "MAX_INVALID_ACTIONS", "RunOutcome"]
