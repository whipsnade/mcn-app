import json
import logging
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.brainstorm.parameters import BRAINSTORM_PARAMETERS
from app.brainstorm.schemas import (
    BrainstormModelOutput,
    BrainstormOutcome,
    BrainstormProfile,
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


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


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
        workspace = await workspace_service.get_owned_session(user_id, session_id, for_update=True)
        user_message = await workspace_service.append_message(
            user_id, session_id, MessageCreate(content=payload.content)
        )
        turn_id = str(payload.turn_id)
        user_message.metadata_json = {"turn_id": turn_id}
        await self.db.flush()
        try:
            await self.thinking_service.bind_turn(
                turn_id=turn_id,
                user_id=user_id,
                session_id=session_id,
                task_id=None,
                trigger_message_id=user_message.id,
            )
        except Exception:
            logger.warning(
                "brainstorm_thinking_bind_failed session_id=%s",
                session_id,
                exc_info=True,
            )
        profile = BrainstormProfile.model_validate(
            (workspace.filters_snapshot or {}).get("brainstorm_profile") or {}
        )
        messages = await workspace_service.list_messages(user_id, session_id)
        recent_messages = compress_messages(messages, max_chars=24_000)
        output = await self._complete(
            profile,
            recent_messages,
            user_id,
            session_id,
            turn_id=turn_id,
        )

        merged = merge_profile(profile, output.extracted)
        ready = bool(output.ready)
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
            goal_specs = None
            if get_settings().goal_planner_enforce_enabled:
                goal_specs = await self._plan_task_goals(
                    user_id=user_id,
                    session_id=session_id,
                    content=payload.content,
                    turn_id=turn_id,
                )
            task = await TaskService(self.db).create(
                user_id,
                session_id,
                TaskCreate(content=payload.content),
                trigger_message_id=user_message.id,
                goal_specs=goal_specs,
            )
            task_id = task.id
            try:
                await self.thinking_service.bind_turn(
                    turn_id=turn_id,
                    user_id=user_id,
                    session_id=session_id,
                    task_id=task.id,
                    trigger_message_id=user_message.id,
                )
            except Exception:
                logger.warning(
                    "brainstorm_thinking_bind_failed task_id=%s",
                    task.id,
                    exc_info=True,
                )
        workspace.updated_at = utc_now()
        workspace.last_accessed_at = workspace.updated_at

        options: list[str] = []
        if not ready and output.question is not None:
            options = list(output.question.options)
        brainstorm_metadata: dict = {
            "ready": ready,
            "options": options,
            "multi": output.question.multi if output.question is not None else False,
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
                max_tokens=2048,
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
