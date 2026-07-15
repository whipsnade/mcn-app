from __future__ import annotations

import json

from app.mcp_gateway.validation import McpValidationError, validate_input
from app.model.contracts import ChatMessage, ModelAdapter, StructuredModelRequest
from app.model.prompts import PLANNER_PROMPT
from app.orchestration.batching import build_execution_batches
from app.orchestration.schemas import PlanValidationError, PlannerContext, ToolPlan


class Planner:
    def __init__(self, *, model: ModelAdapter) -> None:
        self._model = model

    async def plan(self, context: PlannerContext) -> ToolPlan:
        result = await self._model.complete_json(
            StructuredModelRequest(
                purpose="planner",
                template_name=PLANNER_PROMPT.name,
                messages=(
                    ChatMessage(role="system", content=PLANNER_PROMPT.system),
                    ChatMessage(
                        role="user",
                        content=json.dumps(
                            context.model_dump(mode="json"),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    ),
                ),
                output_model=ToolPlan,
            )
        )
        plan = result.value
        self._validate(plan, context)
        return plan

    @staticmethod
    def _validate(plan: ToolPlan, context: PlannerContext) -> None:
        if len(plan.steps) > 10:
            raise PlanValidationError("TOO_MANY_TOOL_CALLS")
        if any(platform not in context.allowed_channels for platform in context.brief.platforms):
            raise PlanValidationError("CHANNEL_NOT_ALLOWED")

        tools = {tool.internal_name: tool for tool in context.tools}
        for step in plan.steps:
            tool = tools.get(step.internal_tool_name)
            if tool is None:
                raise PlanValidationError("TOOL_NOT_ALLOWED")
            try:
                validate_input(step.arguments, tool.input_schema)
            except McpValidationError as exc:
                raise PlanValidationError("INVALID_TOOL_ARGUMENTS") from exc
        build_execution_batches(plan)


__all__ = ["PlanValidationError", "Planner"]
