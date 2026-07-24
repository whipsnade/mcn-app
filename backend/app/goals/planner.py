from __future__ import annotations

import json

from app.goals.context import GoalPlannerContext, GoalPlannerContextBuilder
from app.goals.schemas import GoalPlannerOutput
from app.goals.validation import GoalPlanSemanticError, validate_goal_plan
from app.model.contracts import ChatMessage, ModelAdapter, StructuredModelRequest
from app.model.prompts import GOAL_PLANNER_PROMPT


class GoalPlannerService:
    def __init__(
        self,
        *,
        model: ModelAdapter,
        context_builder: GoalPlannerContextBuilder | None,
    ) -> None:
        self._model = model
        self._context_builder = context_builder

    async def plan_task(self, task_id: str) -> GoalPlannerOutput:
        if self._context_builder is None:
            raise RuntimeError("goal_planner_context_builder_required")
        context = await self._context_builder.build(task_id)
        return await self.plan_context(context)

    async def plan_context(self, context: GoalPlannerContext) -> GoalPlannerOutput:
        payload = {
            "current_message": context.current_message,
            "recent_messages": [
                message.model_dump(mode="json") for message in context.recent_messages
            ],
            "session_context": context.session_context,
            "account_default_brand": context.account_default_brand,
            "artifact_summaries": list(context.artifact_summaries),
            "exemplars": list(context.exemplars),
            "allowed_goal_types": list(context.allowed_goal_types),
        }
        messages = [
            ChatMessage(role="system", content=GOAL_PLANNER_PROMPT.system),
            ChatMessage(
                role="user",
                content=json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        ]
        base_log_context = {
            "user_id": context.user_id,
            "session_id": context.session_id,
            "task_id": context.task_id,
        }
        for attempt in (1, 2):
            log_context = {
                **base_log_context,
                "tags": [
                    "goal_planner:shadow",
                    f"goal_planner:attempt:{attempt}",
                ],
            }
            result = await self._model.complete_json(
                StructuredModelRequest(
                    purpose="goal_planner",
                    template_name=GOAL_PLANNER_PROMPT.name,
                    messages=tuple(messages),
                    output_model=GoalPlannerOutput,
                    max_tokens=2048,
                    log_context=log_context,
                )
            )
            try:
                session_brand = context.session_context.get("active_brand")
                validate_goal_plan(
                    result.value,
                    context.current_message,
                    session_brand=(
                        session_brand if isinstance(session_brand, str) else None
                    ),
                    account_default_brand=context.account_default_brand,
                )
            except GoalPlanSemanticError as error:
                if attempt == 2:
                    raise
                messages.append(
                    ChatMessage(
                        role="user",
                        content=json.dumps(
                            {
                                "validation_error": error.code,
                                "instruction": "修正后重新输出完整 GoalPlannerOutput。",
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    )
                )
                continue
            return result.value
        raise RuntimeError("unreachable")
