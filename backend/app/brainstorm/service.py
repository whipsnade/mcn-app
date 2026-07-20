import json
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
from app.model.contracts import ChatMessage, ModelAdapter, StructuredModelRequest
from app.model.prompts import BRAINSTORM_PROMPT
from app.orchestration.context import compress_messages
from app.orchestration.schemas import PlannerMessage
from app.tasks.schemas import TaskCreate
from app.tasks.service import TaskService
from app.workspace.models import Message
from app.workspace.router import message_read
from app.workspace.schemas import MessageCreate
from app.workspace.service import WorkspaceService, is_default_session_title


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class BrainstormService:
    """澄清阶段的同步问答：请求线程内完成一次模型调用并落库问答消息。"""

    def __init__(self, db: AsyncSession, model: ModelAdapter) -> None:
        self.db = db
        self.model = model

    async def respond(
        self, user_id: str, session_id: str, payload: BrainstormRequest
    ) -> BrainstormOutcome:
        workspace_service = WorkspaceService(self.db)
        workspace = await workspace_service.get_owned_session(user_id, session_id, for_update=True)
        user_message = await workspace_service.append_message(
            user_id, session_id, MessageCreate(content=payload.content)
        )
        profile = BrainstormProfile.model_validate(
            (workspace.filters_snapshot or {}).get("brainstorm_profile") or {}
        )
        messages = await workspace_service.list_messages(user_id, session_id)
        recent_messages = compress_messages(messages, max_chars=24_000)
        output = await self._complete(profile, recent_messages)

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
            task = await TaskService(self.db).create(
                user_id,
                session_id,
                TaskCreate(content=payload.content),
                trigger_message_id=user_message.id,
            )
            task_id = task.id
        workspace.updated_at = utc_now()
        workspace.last_accessed_at = workspace.updated_at

        options: list[str] = []
        if not ready and output.question is not None:
            options = list(output.question.options)
        brainstorm_metadata: dict = {
            "ready": ready,
            "options": options,
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

    async def _complete(
        self, profile: BrainstormProfile, recent_messages: tuple[PlannerMessage, ...]
    ) -> BrainstormModelOutput:
        user_content = json.dumps(
            {
                "messages": [message.model_dump(mode="json") for message in recent_messages],
                "current_profile": profile.model_dump(mode="json"),
                "parameter_checklist": list(BRAINSTORM_PARAMETERS),
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
            )
        )
        return result.value
