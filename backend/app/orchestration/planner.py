from __future__ import annotations

import json

from app.mcp_gateway.contracts import DataTapService
from app.mcp_gateway.validation import McpValidationError, validate_input
from app.model.contracts import ChatMessage, ModelAdapter, StructuredModelRequest
from app.model.prompts import PLANNER_PROMPT, REPLANNER_PROMPT
from app.orchestration.batching import build_execution_batches
from app.orchestration.schemas import PlanValidationError, PlannerContext, ReplanContext, ToolPlan


_SERVICE_CHANNELS: dict[DataTapService, frozenset[str]] = {
    DataTapService.INSIGHT_CUBE: frozenset(
        {"xiaohongshu", "douyin", "bilibili", "weibo", "wechat"}
    ),
    DataTapService.SOCIAL_GROW: frozenset({"xiaohongshu", "douyin", "weibo", "wechat"}),
    DataTapService.SOCIAL_GROW_CONTENT: frozenset(
        {"xiaohongshu", "douyin", "weibo", "wechat"}
    ),
    DataTapService.AKTOOLS: frozenset(
        {"xiaohongshu", "douyin", "bilibili", "weibo", "wechat"}
    ),
    DataTapService.BILIBILI: frozenset({"bilibili"}),
}
_DATATAP_XIAOHONGSHU_SEARCH = "datatap.xiaohongshu.kol.search.v1"
_DATATAP_DOUYIN_SEARCH = "datatap.douyin.kol.search.v1"
_DATATAP_SEARCH_BY_PLATFORM = {
    "xiaohongshu": _DATATAP_XIAOHONGSHU_SEARCH,
    "douyin": _DATATAP_DOUYIN_SEARCH,
}
_ZHEJIANG_LOCATION_TERMS = ("浙江", "湖州")
_MAX_CANDIDATES_PER_PLATFORM = 10


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
        plan = self._compile_supported_search_defaults(result.value, context)
        self._validate(plan, context)
        return plan

    async def replan(self, context: PlannerContext, recovery: ReplanContext) -> ToolPlan:
        if recovery.remaining_calls <= 0:
            raise PlanValidationError("REPLAN_CALL_BUDGET_EXHAUSTED")
        result = await self._model.complete_json(
            StructuredModelRequest(
                purpose="replanner",
                template_name=REPLANNER_PROMPT.name,
                messages=(
                    ChatMessage(role="system", content=REPLANNER_PROMPT.system),
                    ChatMessage(
                        role="user",
                        content=json.dumps(
                            {
                                "planner_context": context.model_dump(mode="json"),
                                "recovery": recovery.model_dump(mode="json"),
                            },
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
        if len(plan.steps) > recovery.remaining_calls:
            raise PlanValidationError("REPLAN_CALL_BUDGET_EXCEEDED")
        prohibited_ids = set(recovery.completed_step_ids) | {
            failure.step_id for failure in recovery.failed_steps
        }
        if any(step.id in prohibited_ids for step in plan.steps):
            raise PlanValidationError("REPLAN_REUSES_COMPLETED_STEP")
        self._validate(plan, context)
        return plan

    @staticmethod
    def _compile_supported_search_defaults(plan: ToolPlan, context: PlannerContext) -> ToolPlan:
        """补齐已审核工具能稳定表达、且来自用户输入的筛选条件。

        这不是替模型补造数据：它只将会话中明确出现的品牌、近 30 天活跃
        和湖州/浙江地区诉求，规范化为 DataTap 小红书工具实际支持的字段。
        """
        source_text = "\n".join(message.content for message in context.recent_messages)
        filters = context.brief.filters
        location_values = filters.get("target_fan_locations", [])
        locations = [str(value) for value in location_values] if isinstance(location_values, list) else []
        has_zhejiang_requirement = any(
            term in source_text or any(term in location for location in locations)
            for term in _ZHEJIANG_LOCATION_TERMS
        )
        has_recent_activity_requirement = any(
            term in source_text for term in ("活跃", "近30天", "最近30天", "30天")
        )
        target_audience = context.brief.target_audience
        requires_female_audience = "女性" in target_audience
        requires_20_to_30_audience = "20" in target_audience and "30" in target_audience
        compiled_steps: list = []
        changed = False
        for step in plan.steps:
            if step.internal_tool_name not in _DATATAP_SEARCH_BY_PLATFORM.values():
                compiled_steps.append(step)
                continue
            arguments = dict(step.arguments)
            raw_request = arguments.get("request")
            if raw_request is not None and not isinstance(raw_request, dict):
                compiled_steps.append(step)
                continue
            request = dict(raw_request or {})
            request.setdefault("page", 1)
            requested_size = request.get("size")
            # 单个平台最多保留 10 位候选：这与产品的 Top10 交付一致，且
            # 避免外部工具返回过大的原始结果而触发安全输出上限。
            request["size"] = (
                min(requested_size, _MAX_CANDIDATES_PER_PLATFORM)
                if isinstance(requested_size, int) and requested_size >= 1
                else _MAX_CANDIDATES_PER_PLATFORM
            )
            if requires_female_audience:
                request["sexListFan"] = ["femalePercentFan"]
            if requires_20_to_30_audience:
                # DataTap 小红书只有 18–24 和 25–34 两个相邻分桶；这是
                # 对 20–30 的最小可解释近似，报告仍会保留分桶限制。
                request["ageListFan"] = ["age2PercentFan", "age3PercentFan"]
            if request.get("sexListFan"):
                request.setdefault("sexListFanMin", 0.5)
            if request.get("ageListFan"):
                request.setdefault("ageListFanMin", 0.2)
            if has_recent_activity_requirement:
                request.setdefault("sumpostMin", 1)
            if has_zhejiang_requirement:
                request["kwProvinceList"] = ["浙江省"]
            if not request.get("textContentWord"):
                if context.brief.brand and not request.get("brandMentionsTag"):
                    request["textContentWord"] = context.brief.brand
                elif context.brief.category:
                    request.pop("categoryMentionsTag", None)
                    request["textContentWord"] = context.brief.category
            compiled = {**arguments, "request": request}
            compiled_steps.append(step.model_copy(update={"arguments": compiled}))
            changed = changed or compiled != step.arguments
        available_tools = {tool.internal_name for tool in context.tools}
        existing_tools = {step.internal_tool_name for step in compiled_steps}
        template_arguments = next(
            (
                step.arguments
                for step in compiled_steps
                if step.internal_tool_name in _DATATAP_SEARCH_BY_PLATFORM.values()
            ),
            {"request": {}},
        )
        next_step_number = len(compiled_steps) + 1
        for platform in context.brief.platforms:
            internal_name = _DATATAP_SEARCH_BY_PLATFORM.get(platform)
            if internal_name is None or internal_name not in available_tools or internal_name in existing_tools:
                continue
            while any(step.id == f"step_{next_step_number}" for step in compiled_steps):
                next_step_number += 1
            compiled_steps.append(
                plan.steps[0].model_copy(
                    update={
                        "id": f"step_{next_step_number}",
                        "internal_tool_name": internal_name,
                        "arguments": dict(template_arguments),
                        "depends_on": (),
                        "evidence_goal": f"{platform} 候选达人",
                    }
                )
            )
            existing_tools.add(internal_name)
            next_step_number += 1
            changed = True
        return plan.model_copy(update={"steps": tuple(compiled_steps)}) if changed else plan

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
            supported_channels = _SERVICE_CHANNELS.get(tool.service, frozenset())
            if not supported_channels.intersection(context.allowed_channels):
                raise PlanValidationError("SERVICE_CHANNEL_NOT_ALLOWED")
            try:
                validate_input(step.arguments, tool.input_schema)
            except McpValidationError as exc:
                raise PlanValidationError("INVALID_TOOL_ARGUMENTS") from exc
        build_execution_batches(plan)


__all__ = ["PlanValidationError", "Planner"]
