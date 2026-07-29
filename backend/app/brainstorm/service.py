import json
import logging
import re
from datetime import UTC, date, datetime
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.brainstorm.parameters import BRAINSTORM_PARAMETERS
from app.brainstorm.schemas import (
    BrainstormModelOutput,
    BrainstormOutcome,
    BrainstormProfile,
    BrainstormQuestion,
    BrainstormRequest,
    merge_profile,
)
from app.core.config import get_settings
from app.goals.context import GoalPlannerContextBuilder
from app.goals.planner import GoalPlannerService
from app.model.contracts import ChatMessage, ModelAdapter, StructuredModelRequest
from app.model.exemplars import find_success_exemplars
from app.model.prompts import BRAINSTORM_PROMPT
from app.orchestration.context import compress_messages
from app.orchestration.schemas import PlannerMessage
from app.tasks.schemas import TaskCreate
from app.tasks.service import TaskService
from app.thinking.contracts import ThinkingOperationSpec
from app.thinking.service import SessionThinkingService, get_session_thinking_service
from app.workspace.models import Message
from app.workspace.router import message_read
from app.workspace.schemas import MessageCreate
from app.workspace.service import WorkspaceService, is_default_session_title


logger = logging.getLogger(__name__)

_KOL_INTENT_PATTERN = re.compile(r"达人|kol|圈选|投放|主播|博主", re.IGNORECASE)
_SCORE_PROFILE_FIELDS: tuple[tuple[str, str], ...] = (
    ("industry", "目标行业"),
    ("regions", "目标地区"),
    ("age_ranges", "目标年龄段"),
)
# 评分 v2 目标年龄段固定档位（与 selection/scoring_v2 的标准桶一致）。
_AGE_RANGE_BUCKETS: list[str] = ["<18", "18-24", "25-34", "35-44", "45+"]
_AGE_QUESTION_PATTERN = re.compile(r"年龄|岁")


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def missing_score_target_profile(profile: BrainstormProfile) -> tuple[str, ...]:
    """仅达人意图校验评分画像；缺项保持严格 0 分前先阻止建任务。"""
    intent_text = " ".join(
        value for value in (profile.goal, profile.kol_filters) if isinstance(value, str)
    )
    if _KOL_INTENT_PATTERN.search(intent_text) is None:
        return ()
    return tuple(
        label
        for field, label in _SCORE_PROFILE_FIELDS
        if not (value := getattr(profile, field))
        or (isinstance(value, str) and not value.strip())
    )


def score_target_profile_question(profile: BrainstormProfile, missing: tuple[str, ...]) -> tuple[str, list[str], bool]:
    """模型违反 ready 契约时的无模型兜底，保证不会越过评分画像直接建任务。"""
    label = missing[0]
    if label == "目标行业":
        options = [profile.category] if profile.category else []
        return "为了按目标画像评分，还需要先确认目标行业。", options, False
    if label == "目标地区":
        return "为了按目标画像评分，还需要确认目标地区（可多选）。", [], True
    return "为了按目标画像评分，还需要确认目标年龄段（可多选）。", list(_AGE_RANGE_BUCKETS), True


class BrainstormService:
    """澄清阶段的同步问答：请求线程内完成一次模型调用并落库问答消息。"""

    def __init__(
        self,
        db: AsyncSession,
        model: ModelAdapter,
        thinking_service: SessionThinkingService | None = None,
    ) -> None:
        self.db = db
        self.model = model
        self.thinking_service = thinking_service or get_session_thinking_service()

    async def respond(
        self, user_id: str, session_id: str, payload: BrainstormRequest
    ) -> BrainstormOutcome:
        workspace_service = WorkspaceService(self.db)
        turn_id = str(payload.turn_id)

        # ── 读阶段：无锁、无写，模型调用全程不持有任何行锁（锁窗口收窄，
        # 见 docs/superpowers/specs/2026-07-27-brainstorm-lock-window-design.md）。
        workspace = await workspace_service.get_owned_session(
            user_id, session_id, for_update=False
        )
        profile = BrainstormProfile.model_validate(
            (workspace.filters_snapshot or {}).get("brainstorm_profile") or {}
        )
        messages = await workspace_service.list_messages(user_id, session_id)
        # 用户消息尚未落库：镜像 GoalPlannerContextBuilder，把当前消息拼到压缩列表尾部。
        recent_messages = list(compress_messages(messages, max_chars=24_000))
        tail_sequence = recent_messages[-1].sequence + 1 if recent_messages else 1
        recent_messages.append(
            PlannerMessage(role="user", content=payload.content, sequence=tail_sequence)
        )
        try:
            await self.thinking_service.bind_turn(
                turn_id=turn_id,
                user_id=user_id,
                session_id=session_id,
                task_id=None,
                trigger_message_id=None,
            )
        except Exception:
            logger.warning(
                "brainstorm_thinking_bind_failed session_id=%s",
                session_id,
                exc_info=True,
            )
        output = await self._complete(
            profile,
            tuple(recent_messages),
            user_id,
            session_id,
            turn_id=turn_id,
        )
        # 模型只负责对话式采集；ready 仍需由服务端守住，避免评分画像缺项时
        # 直接进入会扣 MCP 积分的 KOL 任务。
        candidate_profile = merge_profile(profile, output.extracted)
        missing = missing_score_target_profile(candidate_profile)
        if output.ready and missing:
            message, options, multi = score_target_profile_question(candidate_profile, missing)
            output = output.model_copy(
                update={
                    "ready": False,
                    "assistant_message": message,
                    "question": BrainstormQuestion(text=message, options=options, multi=multi),
                }
            )
        ready = bool(output.ready)
        goal_specs = None
        if ready and get_settings().goal_planner_enforce_enabled:
            goal_specs = await self._plan_task_goals(
                user_id=user_id,
                session_id=session_id,
                content=payload.content,
                turn_id=turn_id,
            )

        # ── 写阶段：短事务，锁窗口内只写不跑模型。
        workspace = await workspace_service.get_owned_session(
            user_id, session_id, for_update=True
        )
        # 重读画像：并发请求可能已在模型调用期间推进画像，以最新值为 merge base。
        # identity map 不会用新行覆盖未过期实例，须显式 locking refresh 才真读到新值。
        await self.db.refresh(workspace, with_for_update=True)
        profile = BrainstormProfile.model_validate(
            (workspace.filters_snapshot or {}).get("brainstorm_profile") or {}
        )
        merged = merge_profile(profile, output.extracted)
        # 并发请求在模型调用期间可能推进 goal/kol_filters，锁内再校验一次。
        missing = missing_score_target_profile(merged)
        if ready and missing:
            message, options, multi = score_target_profile_question(merged, missing)
            output = output.model_copy(
                update={
                    "ready": False,
                    "assistant_message": message,
                    "question": BrainstormQuestion(text=message, options=options, multi=multi),
                }
            )
            ready = False
            goal_specs = None
        user_message = await workspace_service.append_message(
            user_id, session_id, MessageCreate(content=payload.content)
        )
        # 思考持久化靠这个 metadata 键匹配用户消息，非 ready 路径无兜底，必须显式写。
        user_message.metadata_json = {"turn_id": turn_id}
        await self.db.flush()
        filters = dict(workspace.filters_snapshot or {})
        filters["brainstorm_profile"] = merged.model_dump(mode="json")
        workspace.filters_snapshot = filters
        if is_default_session_title(workspace.title) and output.title_suggestion.strip():
            workspace.title = output.title_suggestion.strip()

        task_id = None
        if ready:
            # ready 时把已确认画像写回标量列（截断到列宽，避免长文本写库失败）。
            if merged.brand:
                workspace.brand = merged.brand[:100]
            if merged.category:
                workspace.category = merged.category[:100]
            if merged.platforms:
                workspace.platforms = list(merged.platforms)
            if merged.audience:
                workspace.target_audience = merged.audience[:500]
            task = await TaskService(self.db).create(
                user_id,
                session_id,
                TaskCreate(content=payload.content),
                trigger_message_id=user_message.id,
                goal_specs=goal_specs,
            )
            task_id = task.id
        workspace.updated_at = utc_now()
        workspace.last_accessed_at = workspace.updated_at

        options: list[str] = []
        multi = output.question.multi if output.question is not None else False
        if not ready and output.question is not None:
            options = list(output.question.options)
            # 确定性兜底：评分画像缺目标年龄段且模型问的是年龄问题但 options 为空
            #（模型常把档位写进问题文本），按固定档位注入并强制多选。
            if (
                not options
                and not merged.age_ranges
                and _AGE_QUESTION_PATTERN.search(output.question.text)
            ):
                options = list(_AGE_RANGE_BUCKETS)
                multi = True
        brainstorm_metadata: dict = {
            "ready": ready,
            "options": options,
            "multi": multi,
            "profile_summary": merged.model_dump(mode="json"),
        }
        assistant_metadata: dict = {"brainstorm": brainstorm_metadata}
        if task_id is not None:
            assistant_metadata["task_id"] = task_id
        max_sequence = await self.db.scalar(
            select(func.max(Message.sequence)).where(Message.session_id == session_id)
        )
        assistant_message = Message(
            id=str(uuid4()),
            session_id=session_id,
            user_id=user_id,
            role="assistant",
            content=output.assistant_message,
            sequence=(max_sequence or 0) + 1,
            metadata_json=assistant_metadata,
            created_at=utc_now(),
        )
        self.db.add(assistant_message)
        await self.db.flush()
        try:
            # 补绑 trigger/task（bind_turn 幂等更新绑定）。
            await self.thinking_service.bind_turn(
                turn_id=turn_id,
                user_id=user_id,
                session_id=session_id,
                task_id=task_id,
                trigger_message_id=user_message.id,
            )
        except Exception:
            logger.warning(
                "brainstorm_thinking_bind_failed session_id=%s",
                session_id,
                exc_info=True,
            )
        return BrainstormOutcome(
            ready=ready,
            task_id=task_id,
            message=message_read(assistant_message),
            profile=merged,
        )

    async def _plan_task_goals(
        self,
        *,
        user_id: str,
        session_id: str,
        content: str,
        turn_id: str,
    ) -> list[dict] | None:
        """enforce 规划；clarify/respond/失败一律回退 None（默认 kol_selection）。"""
        thinking_sink = self._thinking_sink(
            ThinkingOperationSpec(
                operation_id=str(uuid4()),
                turn_id=turn_id,
                session_id=session_id,
                user_id=user_id,
                purpose="goal_planner",
                label="正在规划分析目标",
            )
        )
        try:
            context = await GoalPlannerContextBuilder().build_for_message(
                user_id, session_id, content, db=self.db
            )
            output = await GoalPlannerService(
                model=self.model, context_builder=None
            ).plan_context(context, thinking_sink=thinking_sink)
        except LookupError:
            raise
        except Exception:
            logger.warning(
                "brainstorm_goal_planner_fallback session_id=%s", session_id, exc_info=True
            )
            return None
        if output.action != "execute" or not output.goals:
            logger.info(
                "brainstorm_goal_planner_non_execute session_id=%s action=%s question=%s",
                session_id,
                output.action,
                getattr(output.question, "text", None),
            )
            return None
        return [
            {
                "goal_type": goal.goal_type,
                "sequence": goal.sequence,
                "depends_on_sequence": goal.depends_on_sequence,
                "params": goal.params.model_dump(mode="json", exclude_none=True),
            }
            for goal in sorted(output.goals, key=lambda item: item.sequence)
        ]

    async def _complete(
        self,
        profile: BrainstormProfile,
        recent_messages: tuple[PlannerMessage, ...],
        user_id: str,
        session_id: str,
        *,
        turn_id: str,
    ) -> BrainstormModelOutput:
        tags = (
            [f"industry:{profile.category.strip()}"]
            if profile.category and profile.category.strip()
            else []
        )
        exemplars = await find_success_exemplars(
            self.db,
            purpose="brainstorm",
            tags=tags,
            user_id=user_id,
        )
        user_content = json.dumps(
            {
                "current_date": date.today().isoformat(),
                "messages": [message.model_dump(mode="json") for message in recent_messages],
                "current_profile": profile.model_dump(mode="json"),
                "parameter_checklist": list(BRAINSTORM_PARAMETERS),
                "exemplars": exemplars,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        result = await self.model.complete_json(
            StructuredModelRequest(
                purpose="brainstorm",
                template_name=BRAINSTORM_PROMPT.name,
                messages=(
                    ChatMessage(role="system", content=BRAINSTORM_PROMPT.system),
                    ChatMessage(role="user", content=user_content),
                ),
                output_model=BrainstormModelOutput,
                # 推理模型 <think> 占用输出预算，2048 易把 JSON 截断，放大到 6144。
                max_tokens=6144,
                log_context={
                    "user_id": user_id,
                    "session_id": session_id,
                    "tags": tags,
                },
                thinking_sink=self._thinking_sink(
                    ThinkingOperationSpec(
                        operation_id=str(uuid4()),
                        turn_id=turn_id,
                        session_id=session_id,
                        user_id=user_id,
                        purpose="brainstorm",
                        label="正在理解需求",
                    ),
                ),
            )
        )
        return result.value

    def _thinking_sink(self, spec: ThinkingOperationSpec):
        try:
            return self.thinking_service.create_sink(spec)
        except Exception:
            logger.warning(
                "brainstorm_thinking_sink_create_failed session_id=%s",
                spec.session_id,
                exc_info=True,
            )
            return None
