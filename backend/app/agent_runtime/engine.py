"""统一 Session Agent Engine（设计文档 §四 / §4.1 / §七 / §11.3 / Task 14；
v3 加固 §5.4/§5.5/§5.8）。

``AgentEngine.run`` 是模型主导的统一决策循环：反复 build context → model
decide → 分发四种动作（ask_user / call_tool / submit_review / complete），
并施加 Task 3 的 Attempt 保护（30 分钟 / 50 决策）、钱包规则、取消信号与
结构化校验错误。

**引擎是业务无关的**：不维护品牌/活动/KOL 阶段清单、固定工具顺序或
GoalPolicy；模型决定一切业务流程。代码只保留能力边界、状态、计费、证据、
校验与审计。

v3 加固（§5.5）在循环内落实三条不变量：

- **租约心跳**：``RunLeaseHeartbeat``（独立 DB Session，每 lease/3 续租）覆盖
  decide / MCP / Reviewer 长调用全程；发布 Artifact 与写 Run 终态前必须再次
  确认租约持有，丢失则安静退出（不发布、不写终态），交还接管方；
- **取消收口**：每轮循环顶、decide 返回后、工具外发前、Reviewer 返回后检查
  ``cancel_requested``，收口为恰好一个 ``run.cancelled`` 终态事件；decide
  返回后取消已到达时不得落任何 assistant 消息；
- **reviewing 接管**：Run 以 reviewing 进入引擎时（复核期间崩溃恢复），读取
  既有 Review Batch/Item/Attempt 与当前 Draft Revision 继续复核——已 approve
  的 Item 不重审，发布幂等（重复接管不产生重复 Version/事件）。

v3 加固（§5.7）的 Draft/Batch 生命周期不变量：

- 首次 submit_review 创建 Batch 后冻结 Draft 集合与 completion_text；后续提交
  集合不一致回喂 ``review_batch_draft_set_mismatch``，不建/不改 Batch；
- 幻觉/他人 draft_id 转 ``draft_not_found``/``artifact_busy`` 结构化回喂并计入
  无效动作（上限后 Run failed），不整 Run 崩溃；
- ask_user/complete/paused/cancelled/failed 全部非发布出口释放本 Run 持有的
  Draft working head（保留不可变 Revision），Artifact 绝不永久 artifact_busy。

v3 加固（§5.8）的动作协议与事件不变量：

- **allowed_actions 强制**：dispatch 前校验 ``action.action in
  profile.allowed_actions``（如 kol_detail_v1 不允许 ask_user）；违规动作
  作为结构化 ``action_not_allowed`` validation error 回喂并计入无效动作，
  达到统一上限（``MAX_INVALID_ACTIONS``）才收口 failed；
- **非法输出分层**：适配器修复后仍非法的输出以可恢复 ``InvalidModelOutput``
  返回，计入无效动作并回喂；供应商/鉴权/不可恢复协议错误才按系统错误
  ``_fail_run``；
- **thinking 实时事件**：用户可见 Run 由执行层注入
  ``AgentEventThinkingSink``（见 ``thinking_sink_for``），Reviewer/Utility
  内部 Run 不注入；
- **事件顺序**：thinking/tool/review/artifact → assistant message →
  ``message.completed`` → ``run.completed|failed|cancelled``——终态事件是该
  Run 最后一条用户可见事件，在线客户端不会在流关闭前漏收
  ``message.completed``。

G1 收口（§5.8/§15.3）的两条事件契约：

- **终态事件统一收口**：所有使 Run 进入终态的路径（complete / 原子发布 /
  Reviewer reject（含第 3 次 revise 映射）/ 系统失败 ``_fail_run`` / 取消 /
  executor 异常兜底）都经 ``AgentEventStream.append_terminal_once`` 发恰好
  一个终态事件；失败路径携带稳定 ``error_code``（review_rejected /
  review_error / publish_error / model_error / max_invalid_actions /
  run_lease_lost / review_batch_missing / executor_error）。租约已丢失的
  旧 worker 不发终态事件（A4 闸门，接管方负责）；
- **artifact 事件接入统一 Run SSE**：Draft 工具成功发
  ``artifact.draft.created``/``artifact.draft.updated``，原子发布成功为每个
  Artifact 发 ``artifact.published``（``message.completed`` 之前），payload
  带 ``artifact_id/module/parent_artifact_id/status``（发布另带
  ``version``）；kol_detail Run 走同一引擎路径同样覆盖。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Iterable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
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
    AgentArtifact,
    ArtifactDraft,
    ArtifactDraftRevision,
    ArtifactReviewAttempt,
    ArtifactReviewBatch,
    ArtifactReviewItem,
)
from app.agent_artifacts.service import ArtifactBusy, ArtifactService
from app.agent_runtime.context import SessionContextBuilder
from app.agent_runtime.events import AgentEventStream
from app.agent_runtime.heartbeat import RunLeaseHeartbeat
from app.agent_runtime.model_gateway import InvalidModelOutput
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
from app.agent_runtime.reviewer import ReviewBatchDraftSetMismatch, ReviewerDriver
from app.agent_runtime.schemas import FOUR_ACTIONS
from app.agent_runtime.state import InvalidRunTransition, RunStatus
from app.agent_runtime.thinking import AgentEventThinkingSink
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

# Draft 工具 → Run SSE 产物事件（§15.3/G1）：Draft 创建/更新接入统一 Run 事件流，
# 前端据此驱动 artifactsVersion 增长刷新右侧 BI 与未读圆点。
_DRAFT_EVENT_BY_TOOL = {
    "create_draft": "artifact.draft.created",
    "update_draft": "artifact.draft.updated",
}


class _InvalidSubmitReview(Exception):
    """submit_review 的输入类错误（幻觉/他人 draft_id、Batch 集合不一致）。

    只用于送审前的输入校验阶段：引擎捕获后转结构化 validation error 回喂并
    计入无效动作计数，绝不把整 Run 打挂（§5.7/§5.8）。``code`` 为稳定错误码。
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


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
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]] | None = None,
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
        if session_factory is None:
            # 缺省心跳会话与引擎共享（测试注入）；生产必须经 main.engine_factory
            # 传入独立 SessionFactory，心跳续租才能真实提交并对其他连接可见。
            @asynccontextmanager
            async def _shared_session() -> Any:
                yield db

            session_factory = _shared_session
        self._session_factory = session_factory

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
        resume_step: AgentStep | None = None,
        resumed_by: str | None = None,
        user_question: str | None = None,
    ) -> RunOutcome:
        """执行一次用户 Run：直到终态 / clarification / paused / cancelled。

        ``resume_step`` 是 transcript 重建发现的崩溃残留 tool_call Step
        （外发后 / settle 前崩溃）：模型重新发起相同调用时复用它（同一
        ``logical_call_id``，协调器幂等回放，绝不重发、不重复扣费）。
        ``resumed_by`` 区分本次执行是首次启动（None）、用户主动 resume
        （``"user"``）还是系统接管（``"system"``），供 run.resumed 事件归因。
        ``user_question`` 是显式用户问题锚点（G3）：恢复路径由
        ``RunTranscriptLoader`` 从触发消息（或 prompt_snapshot 触发上下文）
        显式给出——恢复后的会话尾部是 tool_result 回放（``role="user"``），
        从消息列表反推会把结构化工具结果误当用户问题（Memory Header /
        Reviewer 上下文被污染）。缺省时兼容反推消息列表最后一条 user 消息
        （首次启动路径：触发消息本身就是唯一 user 消息）。
        """
        conversation = list(messages)
        if user_question is None:
            user_question = self._current_user_question(conversation)
        next_sequence = await self._next_step_sequence(run.id)
        assistant_message_id: str | None = None

        # 租约心跳（§5.5）：独立 DB Session 每 lease/3 续租，覆盖 decide /
        # MCP / Reviewer 长调用全程；心跳丢失后发布/终态写入前的再确认会拦住。
        heartbeat = RunLeaseHeartbeat(
            session_factory=self._session_factory,
            run_id=run.id,
            worker_id=self._worker_id,
            lease_seconds=self._lease_seconds,
        )
        await heartbeat.start()
        try:
            fresh = await self._repo.lock_run(run.id)
            if RunStatus(fresh.status) == RunStatus.REVIEWING:
                # reviewing 接管（复核期间崩溃恢复）：继续未完成的复核——已
                # approve 的 Item 不重审，pending/revise 继续，原子发布幂等。
                await self._events.append(
                    run.id,
                    run.user_id,
                    "run.resumed",
                    {"resumed_by": resumed_by or "system", "reviewing": True},
                )
                settled, published_message_id = await self._resume_reviewing(
                    run, conversation, user_question
                )
                if settled != "revise":
                    return await self._outcome(run, published_message_id)
                # revise：已打回 running 并回喂问题，落入主循环让模型修订。
            else:
                await self._emit_run_started(run, attempt_id, resumed_by=resumed_by)

            consecutive_invalid = 0
            while await self._is_running(run):
                # 续租：长 Attempt（最长 30 分钟）期间租约不能过期，否则 pause 静默
                # 失效、终态迁移抛 run_lease_not_held。续租失败视为不可恢复系统错误。
                if not await self._renew_lease(run):
                    await self._fail_run(run, error_code="run_lease_lost")
                    break
                if await self._guard_attempt_limits(run, attempt_id):
                    # paused 出口释放 Draft working head（§5.7），新 Run/恢复后可接管。
                    await self._release_owned_drafts(run)
                    await self._events.append(
                        run.id, run.user_id, "run.paused", {"attempt_id": attempt_id}
                    )
                    break
                # 取消检查点 1：每轮循环顶。
                if await self._cancel_requested(run):
                    await self._settle_cancelled(run)
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
                    await self._fail_run(run, error_code="model_error")
                    break
                next_sequence += 1
                await self._count_decision(run, attempt_id)

                # §5.8：可恢复非法输出（InvalidModelOutput）、未知动作与 Profile
                # 不允许的动作统一按无效动作计数回喂，达上限才收口 failed。
                validation_error = self._action_validation_error(action, profile)
                if validation_error is not None:
                    consecutive_invalid += 1
                    if consecutive_invalid >= MAX_INVALID_ACTIONS:
                        await self._fail_run(run, error_code="max_invalid_actions")
                        break
                    self._feed_validation_error(
                        conversation, action, message=validation_error
                    )
                    continue
                # 无效计数在「有效交互」完成后清零（call_tool 结算、submit_review
                # 未抛输入错误）：submit_review 输入错误在分发后累计，连续达到
                # MAX_INVALID_ACTIONS 同样收口 failed（§5.7/§5.8）。

                # 取消检查点 2 + 租约再确认（§5.5）：decide（长模型调用）返回后、
                # 动作分发前重查运行态——取消已到达则不得落任何 assistant 产物
                # （消息 / 工具外发 / 提交复核）；租约被接管则安静退出。
                gate = await self._post_decision_gate(run)
                if gate == "cancelled":
                    await self._settle_cancelled(run)
                    break
                if gate == "lease_lost":
                    logger.info(
                        "run %s lease lost after decide; worker %s stops",
                        run.id,
                        self._worker_id,
                    )
                    break

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
                        resume_step=resume_step,
                    )
                    consecutive_invalid = 0  # 有效交互（工具错误也是合法结果）
                    if call_result == "resumed":
                        resume_step = None  # 崩溃残留 Step 已复用收口，只用一次
                    next_sequence += 1
                    if call_result == "cancelled":
                        # decide→dispatch 间隙收到取消：不发起新调用，直接收口 cancelled
                        await self._settle_cancelled(run)
                        break
                    continue
                if action.action == "submit_review":
                    try:
                        settled, published_message_id = await self._handle_submit_review(
                            run=run,
                            action=action,
                            conversation=conversation,
                            user_question=user_question,
                        )
                    except _InvalidSubmitReview as exc:
                        # 幻觉/他人 draft_id、Batch 集合不一致：结构化回喂并计入
                        # 无效动作（§5.7/§5.8），不整 Run 崩溃。
                        consecutive_invalid += 1
                        if consecutive_invalid >= MAX_INVALID_ACTIONS:
                            await self._fail_run(run, error_code="max_invalid_actions")
                            break
                        self._feed_submit_review_error(
                            conversation, code=exc.code, message=exc.message
                        )
                        continue
                    consecutive_invalid = 0  # 送审成功进入复核流程：有效交互
                    if settled == "approved":
                        assistant_message_id = published_message_id or assistant_message_id
                        break
                    if settled in ("rejected", "cancelled", "lease_lost"):
                        break
                    continue  # revise / lineage 错误 → 模型修正后继续
                if action.action == "complete":
                    message = await self._handle_complete(run, action)
                    assistant_message_id = message.id
                    break
        finally:
            await heartbeat.stop()

        return await self._outcome(run, assistant_message_id)

    def thinking_sink_for(self, run: AgentRun) -> AgentEventThinkingSink | None:
        """为用户可见 Run 构造 thinking sink（§5.8/§10.5）；内部 Run 返回 None。

        执行层（executor / KolDetailRunService）为 session_analyst 主 Run 与
        kol_detail Run 注入：模型网关的真实 thinking delta 经 sink 持久化为
        ``thinking.*`` 事件实时 SSE。Reviewer/Utility 内部 Run
        （``visibility != "user"``）不注入，只写 internal Step 审计。
        """
        if run.visibility != "user" or run.run_kind != "user":
            return None
        return AgentEventThinkingSink(
            self._events, run_id=run.id, user_id=run.user_id
        )

    # ------------------------------------------------------------------ #
    # 四种动作
    # ------------------------------------------------------------------ #

    async def _handle_ask_user(self, run: AgentRun, action: Any) -> AgentMessage:
        """ask_user：写 assistant 澄清消息 + pending Memory，Run 以 clarification 收尾。

        §5.7：非发布出口释放本 Run 持有的 Draft working head（保留不可变
        Revision），新 Run 可基于历史 Revision 继续，不得永久 artifact_busy。
        """
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
        await self._release_owned_drafts(run)
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
        resume_step: AgentStep | None = None,
    ) -> str:
        """call_tool：可见性校验 → 外发前持久化 Step → 执行 → 结果回喂消息列表。

        ``InsufficientPointsError`` 作为结构化工具错误回喂模型（§11.3），
        不崩溃；已 settled 调用正常结算。返回 ``"ok"``（新建 Step 执行）、
        ``"resumed"``（复用崩溃残留 Step 执行）或 ``"cancelled"``
        （decide→dispatch 间隙收到取消，未发起调用）。

        **崩溃重续（§5.4）**：``resume_step`` 是 transcript 重建发现的 running
        残留 Step；当本次动作的工具名与参数与其完全一致时复用该 Step——
        step_id 不变 → ``logical_call_id`` 不变 → 协调器按行幂等回放，
        绝不重复外发、不重复扣费（防重不依赖模型记忆）。
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

        resumed = self._match_resume_step(resume_step, action)
        if resumed is not None:
            # 复用崩溃残留 Step：审计归属切换到当前 Attempt；不重复发
            # tool.started（崩溃前已发出，事件流是持久的）。
            step = resumed
            step.attempt_id = attempt_id
            await self._db.flush()
        else:
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
        # G1/§15.3：Draft 工具成功创建/更新后把产物事件接入统一 Run SSE
        # （tool 事件之后、终态事件之前），kol_detail Run 同样覆盖。
        await self._emit_draft_tool_event(run, action, result)
        self._feed_tool_result(conversation, result)
        return "resumed" if resumed is not None else "ok"

    async def _emit_draft_tool_event(
        self, run: AgentRun, action: Any, result: ToolResult
    ) -> None:
        """Draft 工具成功后把 Draft 生命周期事件接入统一 Run SSE（§15.3/G1）。

        只在工具成功时发：create_draft 发 ``artifact.draft.created``（复用已有
        身份继续写时 revision > 1，与 artifact_events 表口径一致记为
        ``artifact.draft.updated``）、update_draft 发 ``artifact.draft.updated``。
        payload 带 ``artifact_id/module/parent_artifact_id/status``；``version``
        为 Draft revision 号，前端据此归并草稿版本并驱动 artifactsVersion 增长。
        工具结果摘要是本仓库自有 JSON 契约（CreateDraftTool/UpdateDraftTool 输出）。
        """
        event_type = _DRAFT_EVENT_BY_TOOL.get(action.internal_tool_name)
        if event_type is None or result.status != "success":
            return
        try:
            summary = json.loads(result.safe_summary)
        except (TypeError, ValueError):
            return
        artifact_id = summary.get("artifact_id")
        if not isinstance(artifact_id, str) or not artifact_id:
            return
        artifact = await self._db.get(AgentArtifact, artifact_id)
        if artifact is None:
            return
        revision = summary.get("revision")
        version = revision if isinstance(revision, int) else 0
        if action.internal_tool_name == "create_draft" and version > 1:
            # 复用既有稳定身份继续写（旧 Run 留下的 Draft）：语义上是更新。
            event_type = "artifact.draft.updated"
        await self._events.append(
            run.id,
            run.user_id,
            event_type,
            {
                "artifact_id": artifact.id,
                "module": artifact.module,
                "parent_artifact_id": artifact.parent_artifact_id,
                "status": artifact.status,
                "version": version,
            },
        )

    async def _emit_artifact_published(
        self, run: AgentRun, versions: Iterable[Any]
    ) -> None:
        """原子发布成功后，为每个发布的 Artifact 发一条 ``artifact.published``。

        payload 带 ``artifact_id/module/parent_artifact_id/status`` 与发布
        ``version``（§15.3）；顺序在 message.completed 之前。
        """
        for version in versions:
            artifact = await self._db.get(AgentArtifact, version.artifact_id)
            if artifact is None:  # pragma: no cover - FK 保证稳定身份存在
                continue
            await self._events.append(
                run.id,
                run.user_id,
                "artifact.published",
                {
                    "artifact_id": artifact.id,
                    "module": artifact.module,
                    "parent_artifact_id": artifact.parent_artifact_id,
                    # publish_batch 已把稳定身份置为 published
                    "status": artifact.status,
                    "version": version.version,
                },
            )

    async def _handle_submit_review(
        self,
        *,
        run: AgentRun,
        action: Any,
        conversation: list[ChatMessage],
        user_question: str,
    ) -> tuple[str, str | None]:
        """submit_review：lineage 校验 → 建/复用 Batch → 转 reviewing → 收口。

        返回 ``(settled, assistant_message_id)``：``settled`` 取
        ``approved`` / ``rejected`` / ``cancelled`` / ``lease_lost``（终态
        或交还接管方）或 ``revise``（继续循环）；approve 时附带已发布消息 id。

        送审前的输入错误（幻觉/他人 draft_id、Batch 集合不一致）抛
        ``_InvalidSubmitReview``，由主循环转结构化回喂并计入无效动作。
        """
        try:
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
        except ReviewBatchDraftSetMismatch as exc:
            raise _InvalidSubmitReview(exc.code, str(exc)) from exc
        except ArtifactBusy as exc:
            raise _InvalidSubmitReview(exc.code, str(exc)) from exc
        except LookupError as exc:
            raise _InvalidSubmitReview("draft_not_found", str(exc)) from exc
        await self._repo.transition(
            run.id, RunStatus.REVIEWING, worker_id=self._worker_id
        )
        await self._events.append(
            run.id,
            run.user_id,
            "review.started",
            {"review_batch_id": batch.id, "artifact_ids": list(action.artifact_draft_ids)},
        )
        return await self._settle_review(
            run=run,
            batch=batch,
            conversation=conversation,
            user_question=user_question,
        )

    async def _resume_reviewing(
        self,
        run: AgentRun,
        conversation: list[ChatMessage],
        user_question: str,
    ) -> tuple[str, str | None]:
        """reviewing 接管（§5.5）：读取既有 Batch/Item/Attempt 继续复核。

        复核期间崩溃后，Run 以 reviewing 被新 worker 领取进入引擎。既有
        Review Batch/Item/Attempt 与当前 Draft Revision 是唯一事实来源：
        已 approve 的 Item 由 ``review_pending`` 自动跳过（不重审），
        pending/revise 的继续复核，完成后走原有原子发布（重复接管不产生
        重复 Version/事件）。
        """
        if await self._cancel_requested(run):
            await self._settle_cancelled(run)
            return "cancelled", None
        batch = await self._db.scalar(
            select(ArtifactReviewBatch).where(
                ArtifactReviewBatch.parent_run_id == run.id
            )
        )
        if batch is None or batch.status in ("completed", "failed"):
            # 不变量破坏：reviewing 但无可复核 Batch——按系统错误干净收口，
            # 绝不卡在 reviewing。
            logger.error(
                "reviewing run %s has no active review batch; failing", run.id
            )
            await self._fail_run(run, error_code="review_batch_missing")
            return "failed", None
        return await self._settle_review(
            run=run,
            batch=batch,
            conversation=conversation,
            user_question=user_question,
        )

    async def _settle_review(
        self,
        *,
        run: AgentRun,
        batch: ArtifactReviewBatch,
        conversation: list[ChatMessage],
        user_question: str,
    ) -> tuple[str, str | None]:
        """Reviewer 复核与收口（submit_review 与 reviewing 接管共用）。

        ``settled`` 取值：``approved``（已原子发布）、``rejected``（reject 或
        Reviewer/发布异常，Run failed）、``revise``（打回 running 继续循环）、
        ``cancelled``（Reviewer 返回后发现取消：不发布，释放 Draft 收口）、
        ``lease_lost``（发布前租约丢失：不发布、不写终态，交还接管方）。
        """
        try:
            # 只审需要复核的 Item：已 approve 且 Revision 未变的自动跳过（不重审）。
            results = await self._reviewer.review_pending(
                parent_run=run, batch=batch, user_question=user_question
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("reviewer failed for run %s; failing run", run.id)
            await self._abort_review(run, batch, error_code="review_error")
            return "rejected", None

        # 取消检查点：Reviewer（长调用）返回后——不发布/不打回，释放 Draft
        # working head（idle），收口为恰好一个 run.cancelled 终态事件。
        if await self._cancel_requested(run):
            await self._settle_cancelled(run)
            return "cancelled", None

        decisions = {result.decision for result in results}
        if "reject" in decisions:
            await self._events.append(
                run.id,
                run.user_id,
                "review.rejected",
                {"review_batch_id": batch.id},
            )
            # G1/§5.8：reject 已由 Reviewer 把 Run 迁移 failed（_finalize_reject），
            # 必须补发 run.failed 终态事件——它是该 Run 最后一条用户可见事件，
            # 缺失会让 SSE 流不结束、前端 Run 卡停在中间态。
            await self._events.append_terminal_once(
                run.id,
                run.user_id,
                "run.failed",
                {"outcome": "failed", "error_code": "review_rejected"},
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

        # 全 approve。results 为空说明是重复接管（全部 Item 早已 approve，
        # review.approved 事件此前已发）——幂等：不重复发事件、不重复复核。
        if results:
            await self._events.append(
                run.id, run.user_id, "review.approved", {"review_batch_id": batch.id}
            )
        # 发布前再次确认租约持有（§5.5）：丢失则不发布、不写终态，安静交还
        # 接管方——绝不出现两个 worker 并发发布。
        if not await self._repo.holds_lease(run.id, self._worker_id):
            logger.info(
                "run %s lease lost before publish; worker %s skips publish",
                run.id,
                self._worker_id,
            )
            return "lease_lost", None
        try:
            versions = await self._service.publish_batch(
                batch.id, worker_id=self._worker_id
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("publish_batch failed for run %s; failing run", run.id)
            await self._abort_review(run, batch, error_code="publish_error")
            return "rejected", None
        # G1/§15.3：每个发布的 Artifact 一条 artifact.published，顺序在
        # message.completed 之前；前端据此实时刷新右侧 BI 与未读圆点。
        await self._emit_artifact_published(run, versions)
        # §5.8 事件顺序：assistant message → message.completed → run.completed，
        # 终态事件是该 Run 最后一条用户可见事件（在线客户端不会漏收 message.completed）。
        await self._events.append(
            run.id, run.user_id, "message.completed", {"type": "completion"}
        )
        await self._events.append_terminal_once(
            run.id, run.user_id, "run.completed", {"outcome": "completed"}
        )
        published = await self._latest_assistant_message(run.id)
        return "approved", (published.id if published is not None else None)

    async def _handle_complete(self, run: AgentRun, action: Any) -> AgentMessage:
        """complete（无正式产物）：写 assistant 消息，Run → completed。

        §5.7：非发布出口释放本 Run 持有的 Draft working head（保留不可变
        Revision），不得永久 artifact_busy。
        §5.8 事件顺序：message.completed 先于 run.completed，终态事件最后。
        """
        metadata = {"type": "completion", "suggestions": action.suggestions}
        message = await self._append_message(
            session_id=run.session_id,
            run_id=run.id,
            role="assistant",
            content=action.text,
            metadata=metadata,
        )
        await self._release_owned_drafts(run)
        await self._repo.transition(
            run.id, RunStatus.COMPLETED, worker_id=self._worker_id
        )
        await self._events.append(
            run.id, run.user_id, "message.completed", {"type": "completion"}
        )
        await self._events.append_terminal_once(
            run.id, run.user_id, "run.completed", {"outcome": "completed"}
        )
        return message

    # ------------------------------------------------------------------ #
    # 循环守卫
    # ------------------------------------------------------------------ #

    async def _is_running(self, run: AgentRun) -> bool:
        fresh = await self._repo.lock_run(run.id)
        return fresh.status == RunStatus.RUNNING

    async def _cancel_requested(self, run: AgentRun) -> bool:
        # 纯读检查点（列查询，不经 identity map）：不加行锁。事件 append 是引擎
        # 的提交点（见 AgentEventStream.append），提交后 REPEATABLE-READ 快照已
        # 刷新，纯读即可见到 API 侧已提交的取消标记；若用 FOR UPDATE，会在
        # tool.started 提交后重新持有 Run 行 X 锁（未提交），阻塞 MCP 协调器
        # 独立会话 INSERT agent_tool_calls 的外键父行 S 锁检查（50s 锁等待超时，
        # 基线回归 0a66fa9 引入，G3 真实并发验证暴露）。
        return bool(
            await self._db.scalar(
                select(AgentRun.cancel_requested).where(AgentRun.id == run.id)
            )
        )

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

    async def _fail_run(self, run: AgentRun, *, error_code: str = "run_failed") -> None:
        """把 Run 收口为 failed，并发恰好一个带稳定 error_code 的 run.failed 终态事件。

        正常路径持有租约经 ``transition`` 迁移；续租失败导致的系统错误无法持有
        租约，此时用 ``force_fail`` 干净收口，避免卡在 running + open attempt。
        收口成功（含 force_fail 确认无其他活跃持有者）后释放本 Run 持有的
        Draft working head（§5.7 failed 出口，保留不可变 Revision）。
        已是终态（幂等）或租约被他人活跃持有（接管方负责）时不发事件。
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
            failed = await self._repo.force_fail(run.id, error_code=error_code)
        if failed:
            await self._release_owned_drafts(run, outcome="failed")
            await self._events.append_terminal_once(
                run.id,
                run.user_id,
                "run.failed",
                {"outcome": "failed", "error_code": error_code},
            )

    async def _renew_lease(self, run: AgentRun) -> bool:
        try:
            return await self._repo.renew_lease(
                run.id, self._worker_id, self._lease_seconds
            )
        except Exception:
            logger.exception("lease renewal failed for run %s", run.id)
            return False

    async def _abort_review(
        self, run: AgentRun, batch: ArtifactReviewBatch, *, error_code: str
    ) -> None:
        """Reviewer/发布异常时的收口：先释放 working head，再让 Run 失败。

        ``error_code`` 随 run.failed 终态事件落库（review_error / publish_error），
        前端与排障据此区分失败来源。
        """
        try:
            await self._reviewer.cancel_reviewing(
                run_id=run.id,
                draft_ids=await self._batch_draft_ids(batch),
                outcome="failed",
            )
        except Exception:
            logger.exception("cancel_reviewing failed for run %s", run.id)
        await self._fail_run(run, error_code=error_code)

    async def _settle_cancelled(self, run: AgentRun) -> None:
        """取消收口：释放本 Run 持有的 Draft（idle）→ 迁移 cancelled → 发恰好一个
        run.cancelled 终态事件。

        已被收口（API 立即取消路径或其他 worker）时幂等跳过——同一 Run 全
        局恰好一个 ``run.cancelled`` 事件。
        """
        fresh = await self._repo.lock_run(run.id)
        if RunStatus(fresh.status) == RunStatus.CANCELLED:
            return
        await self._release_owned_drafts(run)
        cancelled = await self._repo.cancel(run.id, run.user_id)
        if cancelled:
            await self._events.append_terminal_once(run.id, run.user_id, "run.cancelled", {})

    async def _release_owned_drafts(self, run: AgentRun, *, outcome: str = "idle") -> None:
        """释放本 Run 持有的全部 Draft working head，保留不可变 Revision（§5.7）。

        ask_user/complete/paused/cancelled 出口用 ``idle``，failed 出口用
        ``failed``——任何非发布出口都不得让 Artifact 永久 artifact_busy。
        """
        drafts = (
            await self._db.scalars(
                select(ArtifactDraft).where(ArtifactDraft.owner_run_id == run.id)
            )
        ).all()
        if not drafts:
            return
        await self._reviewer.cancel_reviewing(
            run_id=run.id,
            draft_ids=[draft.id for draft in drafts],
            outcome=outcome,
        )

    async def _batch_draft_ids(self, batch: ArtifactReviewBatch) -> list[str]:
        """Batch 全部 Item 对应的 Draft id 列表（release/abort 用）。"""
        items = (
            await self._db.scalars(
                select(ArtifactReviewItem).where(
                    ArtifactReviewItem.batch_id == batch.id
                )
            )
        ).all()
        draft_ids: list[str] = []
        for item in items:
            draft = await self._db.scalar(
                select(ArtifactDraft).where(
                    ArtifactDraft.artifact_id == item.artifact_id
                )
            )
            if draft is not None:
                draft_ids.append(draft.id)
        return draft_ids

    async def _post_decision_gate(self, run: AgentRun) -> str:
        """decide 返回后、动作分发前的运行态闸门（§5.5）。

        返回 ``"ok"`` / ``"cancelled"`` / ``"lease_lost"``：取消已到达则禁止
        分发（不落 assistant 消息、不外发工具、不提交复核）；租约被其他
        worker 接管则安静退出（旧 worker 不得再写任何 Run 状态）。
        """
        fresh = await self._repo.lock_run(run.id)
        if fresh.cancel_requested:
            return "cancelled"
        if RunStatus(fresh.status) != RunStatus.RUNNING:
            return "lease_lost"
        if not AgentRunRepository.owns_active_lease(fresh, self._worker_id):
            return "lease_lost"
        return "ok"

    async def _outcome(
        self, run: AgentRun, assistant_message_id: str | None
    ) -> RunOutcome:
        """读取最新 Run 状态组装 RunOutcome（引擎出口统一收口）。"""
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

    @staticmethod
    def _match_resume_step(
        resume_step: AgentStep | None, action: Any
    ) -> AgentStep | None:
        """崩溃残留 Step 复用匹配：工具名与参数完全一致才复用（同一
        ``logical_call_id`` 幂等回放）；不匹配则新建 Step 正常外发。"""
        if resume_step is None:
            return None
        input_json = resume_step.input_json or {}
        if input_json.get("internal_tool_name") != action.internal_tool_name:
            return None
        if (input_json.get("arguments") or {}) != dict(action.arguments):
            return None
        return resume_step

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
    def _action_validation_error(action: Any, profile: AgentProfile) -> str | None:
        """dispatch 前的动作校验（§5.8）；返回 None 表示可分发，否则为回喂消息。

        三类不可分发输入：
        - ``InvalidModelOutput``：适配器修复后仍非法的输出（可恢复结果）；
        - 未知动作名：不在四种动作协议内；
        - Profile 不允许的动作：合法动作但不在 ``profile.allowed_actions``
          （如 kol_detail_v1 不允许 ask_user——点击触发的详情弹层没有回答
          入口，分发了会让 Run 卡死在 clarification）。
        """
        if isinstance(action, InvalidModelOutput):
            return (
                "model output failed schema validation after repair; "
                "return exactly one valid action JSON object"
            )
        name = getattr(action, "action", None)
        if name not in FOUR_ACTIONS:
            return f"invalid or unknown agent action: {name!r}"
        if name not in profile.allowed_actions:
            return (
                f"action {name!r} is not allowed for profile "
                f"{profile.full_name!r}; choose from {sorted(profile.allowed_actions)}"
            )
        return None

    @staticmethod
    def _action_message(action: Any) -> ChatMessage:
        if isinstance(action, BaseModel):
            return ChatMessage(role="assistant", content=action.model_dump_json())
        return ChatMessage(
            role="assistant",
            content=json.dumps(vars(action), ensure_ascii=False, default=str),
        )

    def _feed_validation_error(
        self, conversation: list[ChatMessage], action: Any, *, message: str
    ) -> None:
        """无效动作的结构化回喂（§5.8）：回显动作原文（若有）+ validation_error。

        ``InvalidModelOutput`` 没有可回显的动作对象（适配器修复后仍非法），
        只回喂错误本身；code 供模型与日志分辨违规类型。
        """
        code = "validation_error"
        if isinstance(action, InvalidModelOutput):
            code = "model_output_invalid"
        else:
            # 回显动作原文，模型据此看到自己输出了什么并修正。
            conversation.append(self._action_message(action))
            if getattr(action, "action", None) in FOUR_ACTIONS:
                code = "action_not_allowed"
        conversation.append(
            ChatMessage(
                role="user",
                content=json.dumps(
                    {
                        "error_type": "validation_error",
                        "code": code,
                        "message": message,
                    },
                    ensure_ascii=False,
                ),
            )
        )

    def _feed_submit_review_error(
        self, conversation: list[ChatMessage], *, code: str, message: str
    ) -> None:
        """submit_review 输入错误的结构化回喂（§5.7）：幻觉/他人 draft_id
        （draft_not_found/artifact_busy）与 Batch 集合不一致
        （review_batch_draft_set_mismatch），模型据此修订或结束 Run。"""
        conversation.append(
            ChatMessage(
                role="user",
                content=json.dumps(
                    {
                        "error_type": "validation_error",
                        "code": code,
                        "message": message,
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

        失败返回结构化错误消息（回喂模型），成功返回 None。幻觉 draft_id
        （不存在）抛 ``LookupError``，由上层转 ``draft_not_found`` 回喂并
        计入无效动作。
        """
        draft = await self._db.get(ArtifactDraft, draft_id)
        if draft is None:
            raise LookupError(f"draft {draft_id!r} not found")
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
        """一个用户 Run 最多一条 Review Batch（§8.1）：存在则复用。

        复用前核对冻结的 Draft 集合（§5.7）：首次 submit 后 Batch 的 draft id
        集合与 completion_text 冻结；后续提交集合不一致（新增/遗漏/替换）抛
        ``ReviewBatchDraftSetMismatch``，不建/不改 Batch。
        """
        existing = await self._db.scalar(
            select(ArtifactReviewBatch).where(
                ArtifactReviewBatch.parent_run_id == run.id
            )
        )
        if existing is not None:
            frozen_ids = await self._batch_draft_ids(existing)
            if set(action.artifact_draft_ids) != set(frozen_ids):
                raise ReviewBatchDraftSetMismatch(
                    frozen=sorted(frozen_ids),
                    submitted=sorted(str(d) for d in action.artifact_draft_ids),
                )
            return existing
        return await self._reviewer.create_batch(
            parent_run_id=run.id,
            draft_ids=action.artifact_draft_ids,
            completion_text=action.completion_text,
        )

    # ------------------------------------------------------------------ #
    # 审计辅助
    # ------------------------------------------------------------------ #

    async def _emit_run_started(
        self, run: AgentRun, attempt_id: str, *, resumed_by: str | None = None
    ) -> None:
        attempt = await self._db.get(AgentRunAttempt, attempt_id)
        if attempt is not None and attempt.attempt > 1:
            # resumed_by 由执行器按领取路径归因：用户主动 resume（"user"，
            # NULL 租约沿用 open Attempt）或系统接管（"system"，过期租约
            # pause+重建）；引擎直接调用未归因时按系统接管处理。
            await self._events.append(
                run.id,
                run.user_id,
                "run.resumed",
                {"attempt": attempt.attempt, "resumed_by": resumed_by or "system"},
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
