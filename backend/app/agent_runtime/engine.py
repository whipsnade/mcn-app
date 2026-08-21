"""统一 Session Agent Engine（设计文档 §四 / §4.1 / §七 / §11.3 / Task 14；
v3 加固 §5.4/§5.5/§5.8；直接发布改造 Task 4）。

``AgentEngine.run`` 是模型主导的统一决策循环：反复 build context → model
decide → 分发四种动作（ask_user / call_tool / publish_artifacts / complete），
并施加 Task 3 的 Attempt 保护（30 分钟 / 50 决策）、钱包规则、取消信号与
结构化校验错误。

**引擎是业务无关的**：不维护品牌/活动/KOL 阶段清单、固定工具顺序或
GoalPolicy；模型决定一切业务流程。代码只保留能力边界、状态、计费、证据、
校验与审计。

直接发布协议（Task 4，取代模型 Reviewer）：

- ``publish_artifacts`` 是**非终态**动作：``ArtifactPublicationService``
  逐 Draft 确定性校验并独立事务发布（一项失败不回滚其他成功项），逐项
  发 ``artifact.published`` 与汇总事件 ``artifact.publish.completed``，
  逐项结果回喂后模型继续决策循环（修订重发 / abandon_draft / complete）；
  单项意外异常不静默崩掉循环——回滚会话后按 failed（``publish_error``）
  结果回喂，剩余 Draft 继续处理；
- ``complete`` 有活动 Draft 闸门：本 Run 仍持有 Draft 时回喂结构化错误
  ``ACTIVE_DRAFTS_REMAIN`` 并继续循环，不得留下活动 Draft；
- 终态聚合（设计 §4.2）：至少一个产物发布成功且存在未最终发布的失败/放弃
  记录 → ``completed_with_warnings``（终态，租约/取消/暂停/executor 扫描
  一致对待）；零产物成功且存在失败/放弃项 → ``failed``
  （``ALL_ARTIFACTS_FAILED``）；否则 ``completed``；
- 历史 ``reviewing`` Run（部署前复核期间崩溃）进入引擎时直接收口 failed
  （``LEGACY_REVIEWING_UNSUPPORTED``），保留 Draft，不再启动 Reviewer；
  新执行不创建 Reviewer Run、不写 Review 表、不进入 reviewing。

v3 加固（§5.5）在循环内落实三条不变量：

- **租约心跳**：``RunLeaseHeartbeat``（独立 DB Session，每 lease/3 续租）覆盖
  decide / MCP 长调用全程；发布 Artifact 与写 Run 终态前必须再次
  确认租约持有，丢失则安静退出（不发终态），交还接管方；
- **取消收口**：每轮循环顶、decide 返回后、工具外发前检查
  ``cancel_requested``，收口为恰好一个 ``run.cancelled`` 终态事件；decide
  返回后取消已到达时不得落任何 assistant 消息；
- **发布租约边界**：``ArtifactPublicationService.publish`` 入口复核调用方
  worker 仍持有 Run 活跃租约，丢失则逐项停止（已提交成功项不回滚）。

v3 加固（§5.7）的 Draft 生命周期不变量：

- 幻觉/他人 draft_id 由发布服务逐项转 ``draft_not_found``/``artifact_busy``
  结构化结果回喂，不整 Run 崩溃；
- ask_user/paused/cancelled/failed 全部非发布出口释放本 Run 持有的
  Draft working head（保留不可变 Revision），Artifact 绝不永久 artifact_busy；
  complete 出口由活动 Draft 闸门保证无持有 Draft。

v3 加固（§5.8）的动作协议与事件不变量：

- **allowed_actions 强制**：dispatch 前校验 ``action.action in
  profile.allowed_actions``（如 kol_detail_v1 不允许 ask_user）；违规动作
  作为结构化 ``action_not_allowed`` validation error 回喂并计入无效动作，
  达到统一上限（``MAX_INVALID_ACTIONS``）才收口 failed；
- **非法输出分层**：适配器修复后仍非法的输出以可恢复 ``InvalidModelOutput``
  返回，计入无效动作并回喂；供应商/鉴权/不可恢复协议错误才按系统错误
  ``_fail_run``；
- **thinking 实时事件**：用户可见 Run 由执行层注入
  ``AgentEventThinkingSink``（见 ``thinking_sink_for``），Utility
  内部 Run 不注入；
- **事件顺序**：thinking/tool/artifact → assistant message →
  ``message.completed`` → ``run.completed|completed_with_warnings|failed|
  cancelled``——终态事件是该 Run 最后一条用户可见事件，在线客户端不会在
  流关闭前漏收 ``message.completed``。

G1 收口（§5.8/§15.3）的两条事件契约：

- **终态事件统一收口**：所有使 Run 进入终态的路径（complete / 系统失败
  ``_fail_run`` / 取消 / executor 异常兜底 / 历史 reviewing 收口）都经
  ``AgentEventStream.settle_terminal`` 事务边界收口——Run 状态迁移与恰好
  一个终态事件在同一加锁事务内提交（H1），失败路径携带稳定 ``error_code``
  （model_error / max_invalid_actions / run_lease_lost / executor_error /
  LEGACY_REVIEWING_UNSUPPORTED）。租约已丢失的旧 worker 不发终态事件
  （A4 闸门，接管方负责）；
- **artifact 事件接入统一 Run SSE**：Draft 工具成功发
  ``artifact.draft.created``/``artifact.draft.updated``，直接发布成功为每个
  Artifact 发 ``artifact.published`` 并随 ``publish_artifacts`` 动作发汇总
  事件 ``artifact.publish.completed``（``message.completed`` 之前），payload
  带 ``artifact_id/module/parent_artifact_id/status``（发布另带
  ``version``）；kol_detail Run 走同一引擎路径同样覆盖。发布循环中崩溃、
  接管后直接 complete 的窗口由 complete 前的缺失事件幂等补发兜底。
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

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_artifacts.models import (
    AgentArtifact,
    AgentArtifactVersion,
    ArtifactDraft,
    ArtifactPublishAttempt,
)
from app.agent_artifacts.publishing import ArtifactPublicationService, PublishItemResult
from app.agent_runtime.context import SessionContextBuilder
from app.agent_runtime.events import AgentEventStream
from app.agent_runtime.heartbeat import RunLeaseHeartbeat
from app.agent_runtime.model_gateway import InvalidModelOutput
from app.agent_runtime.models import (
    AgentEvent,
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
from app.agent_runtime.reviewer import release_run_drafts
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
# 前端据此驱动 artifactsVersion 增长刷新右侧 BI 与未读圆点。六个 Builder 工具
# 与 create_draft 同为「建 Draft」语义（H2/H5 起强类型 Artifact 只能走 Builder，
# 产物事件必须同样接入，否则前端丢失草稿就绪信号）。
_DRAFT_EVENT_BY_TOOL = {
    "create_draft": "artifact.draft.created",
    "update_draft": "artifact.draft.updated",
    "build_brand_report_draft": "artifact.draft.created",
    "build_campaign_report_draft": "artifact.draft.created",
    "build_kol_selection_draft": "artifact.draft.created",
    "build_kol_analysis_draft": "artifact.draft.created",
    "build_kol_detail_draft": "artifact.draft.created",
    "build_insight_draft": "artifact.draft.created",
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
    ``events`` 是 Task 4 事件流；发布由确定性 ``ArtifactPublicationService``
    完成（无模型 Reviewer）。``worker_id`` 必须与父 Run 租约持有者一致，
    所有状态迁移才合法。
    """

    def __init__(
        self,
        db: AsyncSession,
        *,
        gateway: Any,
        registry: ToolRegistry,
        events: AgentEventStream,
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
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._repo = repo or AgentRunRepository(db)
        self._publisher = ArtifactPublicationService(db)
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
        从消息列表反推会把结构化工具结果误当用户问题（Memory Header 上下文
        被污染）。缺省时兼容反推消息列表最后一条 user 消息（首次启动路径：
        触发消息本身就是唯一 user 消息）。
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
                # 历史遗留（直接发布改造前复核期间崩溃）：Reviewer 已下线，
                # 不再继续复核——收口 failed 并保留 Draft（不释放、不重建），
                # 恢复扫描/executor 领取的历史 reviewing Run 都在此收口。
                logger.info(
                    "legacy reviewing run %s settles failed; reviewer retired", run.id
                )
                fresh.error_code = "LEGACY_REVIEWING_UNSUPPORTED"
                await self._events.settle_terminal(
                    run.id,
                    run.user_id,
                    RunStatus.FAILED,
                    {
                        "outcome": "failed",
                        "error_code": "LEGACY_REVIEWING_UNSUPPORTED",
                    },
                    worker_id=self._worker_id,
                )
                return await self._outcome(run, None)
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
                # 无效计数在「有效交互」完成后清零（call_tool 结算、publish_artifacts
                # 逐项结果回喂）：连续达到 MAX_INVALID_ACTIONS 收口 failed（§5.8）。

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
                if action.action == "publish_artifacts":
                    dispatch = await self._handle_publish_artifacts(
                        run=run, action=action, conversation=conversation
                    )
                    if dispatch == "lease_lost":
                        # 发布中租约被接管：已提交成功项不回滚，终态交接管方，
                        # 本 worker 安静退出（A4）。
                        logger.info(
                            "run %s lease lost during publish; worker %s stops",
                            run.id,
                            self._worker_id,
                        )
                        break
                    consecutive_invalid = 0  # 发布是有效交互（逐项失败也是合法结果）
                    continue
                if action.action == "complete":
                    # 活动 Draft completion 闸门：complete 不得留下本 Run 拥有的
                    # 活动 Draft——回喂结构化错误，模型发布/放弃后再完成。
                    active_draft_ids = await self._active_owned_draft_ids(run)
                    if active_draft_ids:
                        self._feed_completion_blocked(conversation, active_draft_ids)
                        continue
                    try:
                        message = await self._handle_complete(run, action)
                    except InvalidRunTransition as exc:
                        # Completion gate rejection is a stable business outcome,
                        # not an executor/worker crash.  Preserve its code in the
                        # terminal event and prevent Recovery from treating it as
                        # an infrastructure retry.
                        error_code = str(exc) or "completion_validation_failed"
                        await self._fail_run(run, error_code=error_code)
                        break
                    assistant_message_id = message.id
                    break
        finally:
            await heartbeat.stop()

        return await self._outcome(run, assistant_message_id)

    def thinking_sink_for(self, run: AgentRun) -> AgentEventThinkingSink | None:
        """为用户可见 Run 构造 thinking sink（§5.8/§10.5）；内部 Run 返回 None。

        执行层（executor / KolDetailRunService）为 session_analyst 主 Run 与
        kol_detail Run 注入：模型网关的真实 thinking delta 经 sink 持久化为
        ``thinking.*`` 事件实时 SSE。Utility 内部 Run
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

        只在工具成功时发：create_draft 与五个 Builder 工具发
        ``artifact.draft.created``（复用已有身份继续写时 revision > 1，与
        artifact_events 表口径一致记为 ``artifact.draft.updated``）、
        update_draft 发 ``artifact.draft.updated``。
        payload 带 ``artifact_id/draft_id/module/parent_artifact_id/status``；``version``
        为 Draft revision 号，前端据此归并草稿版本并驱动 artifactsVersion 增长。
        工具结果摘要是本仓库自有 JSON 契约（CreateDraftTool/UpdateDraftTool 与
        Builder 工具的 ``_draft_summary`` 输出同构）。
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
        draft_id = summary.get("draft_id")
        if not isinstance(draft_id, str) or not draft_id:
            return
        artifact = await self._db.get(AgentArtifact, artifact_id)
        if artifact is None:
            return
        revision = summary.get("revision")
        version = revision if isinstance(revision, int) else 0
        if event_type == "artifact.draft.created" and version > 1:
            # 复用既有稳定身份继续写（旧 Run 留下的 Draft / Builder 再构建）：
            # 语义上是更新。
            event_type = "artifact.draft.updated"
        await self._events.append(
            run.id,
            run.user_id,
            event_type,
            {
                "artifact_id": artifact.id,
                "draft_id": draft_id,
                "module": artifact.module,
                "parent_artifact_id": artifact.parent_artifact_id,
                "status": artifact.status,
                "version": version,
            },
        )

    async def _emit_publish_item_events(
        self, run: AgentRun, results: Iterable[PublishItemResult]
    ) -> None:
        """逐项发布事件：每个发布成功的 Artifact 一条 ``artifact.published``。

        payload 带 ``artifact_id/module/parent_artifact_id/status`` 与发布
        ``version``（§15.3）；顺序在 message.completed 之前。
        """
        for item in results:
            if item.status != "published":
                continue
            artifact = await self._db.get(AgentArtifact, item.artifact_id)
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
                    # 发布服务已把稳定身份置为 published
                    "status": artifact.status,
                    "version": item.version,
                },
            )

    async def _emit_missing_published_events(self, run: AgentRun) -> None:
        """崩溃窗口兜底：发布已逐项提交但 ``artifact.published`` 未发出（发布循环
        中崩溃、接管后直接 complete）时，按 source_run_id 幂等补发缺失事件——
        顺序仍在 message.completed 之前，不重复发已提交过的事件。
        """
        versions = list(
            (
                await self._db.scalars(
                    select(AgentArtifactVersion).where(
                        AgentArtifactVersion.source_run_id == run.id
                    )
                )
            ).all()
        )
        if not versions:
            return
        _, published_ids = await self._emitted_event_index(run.id)
        for version in versions:
            if version.artifact_id in published_ids:
                continue
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
                    "status": artifact.status,
                    "version": version.version,
                },
            )

    async def _handle_publish_artifacts(
        self,
        *,
        run: AgentRun,
        action: Any,
        conversation: list[ChatMessage],
    ) -> str:
        """publish_artifacts：确定性发布服务逐 Draft 校验并发布（非终态动作）。

        逐项独立事务（Task 3）：一项失败不回滚其他成功项；每个发布成功的
        Artifact 立即发 ``artifact.published``（缩小崩溃窗口），全部项处理完
        发一条汇总事件 ``artifact.publish.completed``，逐项结果回喂后模型继续
        决策循环（修订重发 / abandon_draft / complete）。

        单项意外异常不静默崩掉循环：捕获后回滚会话、按 ``failed``
        （``publish_error``）结果回喂，剩余 Draft 继续处理。租约丢失
        （``run_lease_not_held``）时返回 ``"lease_lost"`` 交还接管方（A4），
        已提交的成功项不回滚。
        """
        # 快照 run 标量：单项异常后的 rollback 会让 ORM 属性过期，惰性重载在
        # 异常处理上下文里触发 MissingGreenlet。
        run_id = run.id
        user_id = run.user_id
        results: list[PublishItemResult] = []
        for draft_id in dict.fromkeys(str(d) for d in action.artifact_draft_ids):
            try:
                item_results = await self._publisher.publish(
                    run_id=run_id, draft_ids=(draft_id,), worker_id=self._worker_id
                )
            except InvalidRunTransition:
                # 租约丢失（发布入口复核 §5.5）：此前逐项事件已随发布提交发出，
                # 终态交接管方，本 worker 安静退出。
                return "lease_lost"
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "unexpected publish failure for draft %s in run %s",
                    draft_id,
                    run_id,
                )
                await self._db.rollback()
                # rollback 使 ORM 实例属性过期：显式刷新，避免后续惰性重载在
                # 非 await 上下文触发 MissingGreenlet。
                await self._db.refresh(run)
                results.append(
                    PublishItemResult(
                        draft_id=draft_id,
                        status="failed",
                        artifact_id="",
                        artifact_version_id=None,
                        version=None,
                        errors=(
                            {"code": "publish_error", "msg": "unexpected publish failure"},
                        ),
                    )
                )
                continue
            results.extend(item_results)
            # 逐项即时发 artifact.published：发布已独立提交，事件随发随持久化；
            # 崩溃窗口内至多丢失在途一项的事件（complete 前的补发兜底）。
            await self._emit_publish_item_events(run, item_results)
        await self._events.append(
            run_id,
            user_id,
            "artifact.publish.completed",
            {
                "published": sum(1 for r in results if r.status == "published"),
                "validation_failed": sum(
                    1 for r in results if r.status == "validation_failed"
                ),
                "failed": sum(1 for r in results if r.status == "failed"),
                "items": [
                    {
                        "draft_id": r.draft_id,
                        "status": r.status,
                        "artifact_id": r.artifact_id,
                        "version": r.version,
                    }
                    for r in results
                ],
            },
        )
        self._feed_publish_results(conversation, results)
        return "ok"

    @staticmethod
    def _feed_publish_results(
        conversation: list[ChatMessage], results: Iterable[PublishItemResult]
    ) -> None:
        """逐项发布结果回喂模型：published / validation_failed / failed + 结构化错误。"""
        conversation.append(
            ChatMessage(
                role="user",
                content=json.dumps(
                    {
                        "publish_results": [
                            {
                                "draft_id": r.draft_id,
                                "status": r.status,
                                "artifact_id": r.artifact_id,
                                "artifact_version_id": r.artifact_version_id,
                                "version": r.version,
                                "errors": list(r.errors),
                            }
                            for r in results
                        ]
                    },
                    ensure_ascii=False,
                ),
            )
        )

    async def _emitted_event_index(self, run_id: str) -> tuple[set[str], set[str]]:
        """该 Run 已提交事件的类型集合 + artifact.published 的 artifact_id 集合
        （接管幂等去重用）。"""
        rows = (
            await self._db.scalars(select(AgentEvent).where(AgentEvent.run_id == run_id))
        ).all()
        types = {row.event_type for row in rows}
        published_ids = {
            (row.payload_json or {}).get("artifact_id")
            for row in rows
            if row.event_type == "artifact.published"
        }
        return types, {pid for pid in published_ids if pid is not None}

    async def _handle_complete(self, run: AgentRun, action: Any) -> AgentMessage:
        """complete：写 assistant 消息，按发布/失败项聚合终态（设计 §4.2）。

        调用前主循环已过活动 Draft 闸门（本 Run 无持有 Draft）。
        终态聚合：有未最终发布的失败/放弃项或统一 completion validator 返回
        warning → ``completed_with_warnings``；否则 ``completed``。普通用户
        可见 Run 的 validator 同时要求当前 Run 已发布顶层主 Artifact，但不
        固定具体 contract；澄清与 utility Run 走各自的文本出口。
        §5.8 事件顺序：缺失的 artifact.published（崩溃窗口兜底）→
        message.completed → run.completed / run.completed_with_warnings / run.failed。
        H1：Run 迁移与终态事件由 settle_terminal 同一加锁事务提交，
        消除"已终态无事件"窗口。
        """
        metadata = {"type": "completion", "suggestions": action.suggestions}
        await self._emit_missing_published_events(run)
        _published_ids, warning_artifact_ids = await self._publish_outcome_artifact_ids(run)
        # 先持久化本次 completion，再由统一 validator 检查真实 Step/ToolCall
        # 状态；只有门禁通过后才发 message.completed。不能先收口 running Step
        # 再检查，否则 ACK 丢失窗口会被伪装成可完成。
        message = await self._append_message(
            session_id=run.session_id,
            run_id=run.id,
            role="assistant",
            content=action.text,
            metadata=metadata,
        )
        from app.pi_gateway.completion import CompletionValidator

        completion_validator = CompletionValidator(self._db).validate
        validation = await completion_validator(run)
        if not bool(validation):
            code = getattr(validation, "code", None)
            raise InvalidRunTransition(
                code if isinstance(code, str) else "completion_validation_failed"
            )
        validation_warnings = tuple(
            warning for warning in getattr(validation, "warnings", ()) if isinstance(warning, str)
        )

        await self._events.append(
            run.id, run.user_id, "message.completed", {"type": "completion"}
        )
        if warning_artifact_ids or validation_warnings:
            # Builder 失败/放弃不再定义业务失败；Pi 可以降级为文字完成。
            # 平台只把 abandoned Draft、unknown 等一致性限制作为 warning
            # 暴露给 UI/UAT，不能借此推导用户必须交付某种 Artifact。
            warning_payload: dict[str, Any] = {"outcome": "completed_with_warnings"}
            if warning_artifact_ids:
                warning_payload["warning_artifact_ids"] = sorted(warning_artifact_ids)
            if validation_warnings:
                warning_payload["warnings"] = list(validation_warnings)
            await self._events.settle_terminal(
                run.id,
                run.user_id,
                RunStatus.COMPLETED_WITH_WARNINGS,
                warning_payload,
                worker_id=self._worker_id,
                completion_validator=completion_validator,
            )
        else:
            await self._events.settle_terminal(
                run.id,
                run.user_id,
                RunStatus.COMPLETED,
                {"outcome": "completed"},
                worker_id=self._worker_id,
                completion_validator=completion_validator,
            )
        return message

    async def _active_owned_draft_ids(self, run: AgentRun) -> list[str]:
        """本 Run 仍持有的 Draft working head id 列表（活动 Draft completion 闸门）。

        发布成功 / 放弃 / 非发布出口释放都会清空 owner，因此 owner 仍指向本
        Run 即「活动」（含 validation_failed 后保留待修订的 Draft）。
        """
        return list(
            (
                await self._db.scalars(
                    select(ArtifactDraft.id).where(ArtifactDraft.owner_run_id == run.id)
                )
            ).all()
        )

    @staticmethod
    def _feed_completion_blocked(
        conversation: list[ChatMessage], active_draft_ids: list[str]
    ) -> None:
        """complete 闸门的结构化回喂：本 Run 仍有活动 Draft 时不得完成。"""
        conversation.append(
            ChatMessage(
                role="user",
                content=json.dumps(
                    {
                        "error_type": "completion_blocked",
                        "error_code": "ACTIVE_DRAFTS_REMAIN",
                        "active_draft_ids": list(active_draft_ids),
                        "message": (
                            "complete is blocked while drafts owned by this run remain "
                            "active; publish them with publish_artifacts or discard "
                            "them with the abandon_draft tool first"
                        ),
                    },
                    ensure_ascii=False,
                ),
            )
        )

    async def _publish_outcome_artifact_ids(
        self, run: AgentRun
    ) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
        """终态聚合：``(已发布键, 最终失败的键)``，键带域。

        最终失败 = 存在 validation_failed/failed Attempt 且无同域 published
        Attempt（同一 Artifact 先 validation_failed 后修订发布成功不计 warning）；
        引用失败（draft 不存在等拒绝记录，``artifact_id`` 为 NULL）同样参与
        聚合——Run 不得在存在失败发布项时被错误标记为 completed（Gate A
        审查修复）。**键显式区分域**：真实 Artifact 用 ``("artifact", id)``、
        拒绝记录用 ``("rejected_draft", draft_id)``——模型把已发布的
        artifact_id 误当 draft_id 提交时（现实输入错误），拒绝项不会与
        published 项同键而错误消除失败项（Gate A 三审）。
        """
        attempts = (
            await self._db.scalars(
                select(ArtifactPublishAttempt).where(
                    ArtifactPublishAttempt.run_id == run.id
                )
            )
        ).all()

        def _artifact_key(attempt: ArtifactPublishAttempt) -> tuple[str, str]:
            if attempt.artifact_id is not None:
                return ("artifact", attempt.artifact_id)
            rejected = (attempt.validation_json or {}).get("rejected_draft_id")
            key = rejected if isinstance(rejected, str) and rejected else attempt.id
            return ("rejected_draft", key)

        failed = {
            _artifact_key(attempt)
            for attempt in attempts
            if attempt.status in ("validation_failed", "failed")
        }
        published = {
            _artifact_key(attempt)
            for attempt in attempts
            if attempt.status == "published"
        }
        return published, failed - published

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
        H1：迁移只是 flush，与 run.failed 事件由 settle_terminal 同一事务提交。
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
            run.error_code = error_code
            await self._db.flush()
            await self._release_owned_drafts(run, outcome="failed")
            await self._events.settle_terminal(
                run.id,
                run.user_id,
                RunStatus.FAILED,
                {"outcome": "failed", "error_code": error_code},
                worker_id=self._worker_id,
            )

    async def _renew_lease(self, run: AgentRun) -> bool:
        try:
            return await self._repo.renew_lease(
                run.id, self._worker_id, self._lease_seconds
            )
        except Exception:
            logger.exception("lease renewal failed for run %s", run.id)
            return False

    async def _settle_cancelled(self, run: AgentRun) -> None:
        """取消收口：释放本 Run 持有的 Draft（idle）→ 终态事务边界迁移 cancelled
        并发恰好一个 run.cancelled 终态事件。

        已被收口（API 立即取消路径或其他 worker）时幂等跳过——同一 Run 全
        局恰好一个 ``run.cancelled`` 事件。
        """
        fresh = await self._repo.lock_run(run.id)
        if RunStatus(fresh.status) == RunStatus.CANCELLED:
            return
        await self._release_owned_drafts(run)
        await self._events.settle_terminal(
            run.id, run.user_id, RunStatus.CANCELLED, {}, worker_id=self._worker_id
        )

    async def _release_owned_drafts(self, run: AgentRun, *, outcome: str = "idle") -> None:
        """释放本 Run 持有的全部 Draft working head，保留不可变 Revision（§5.7）。

        ask_user/paused/cancelled 出口用 ``idle``，failed 出口用 ``failed``——
        任何非发布出口都不得让 Artifact 永久 artifact_busy（complete 出口由
        活动 Draft 闸门保证无持有 Draft）。
        实现与执行器/恢复循环的取消孤儿收口（I1）共享
        :func:`app.agent_runtime.reviewer.release_run_drafts`。
        """
        await release_run_drafts(self._db, run.id, outcome=outcome)

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
        run = await self._db.scalar(
            select(AgentRun).where(AgentRun.id == run_id).with_for_update()
        )
        if run is None:
            raise LookupError("run_not_found")
        current = await self._db.scalar(
            select(func.max(AgentStep.sequence))
            .where(AgentStep.run_id == run_id)
            .with_for_update()
        )
        return (current or 0) + 1


__all__ = ["MAX_INVALID_ACTIONS", "AgentEngine", "RunOutcome"]
