from __future__ import annotations

import json
from typing import Any

from app.mcp_gateway.contracts import DataTapService
from app.mcp_gateway.validation import McpValidationError, validate_input
from app.model.contracts import ChatMessage, ModelAdapter, ModelAdapterError, StructuredModelRequest
from app.model.prompts import PLANNER_PROMPT, REPLANNER_PROMPT
from app.orchestration.batching import build_execution_batches
from app.orchestration.routing import classify_analysis_request
from app.orchestration.schemas import (
    PlanValidationError,
    PlannerContext,
    ReplanContext,
    ToolPlan,
    ToolPlanStep,
    derive_analysis_scope,
)


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
_DATATAP_BILIBILI_SEARCH = "datatap.social.grow.kol.bilibili.search.v1"
_DATATAP_WEIBO_SEARCH = "datatap.social.grow.kol.weibo.search.v1"
_DATATAP_WECHAT_SEARCH = "datatap.social.grow.kol.wechat.search.v1"
_DATATAP_SEARCH_BY_PLATFORM = {
    "xiaohongshu": _DATATAP_XIAOHONGSHU_SEARCH,
    "douyin": _DATATAP_DOUYIN_SEARCH,
    "bilibili": _DATATAP_BILIBILI_SEARCH,
    "weibo": _DATATAP_WEIBO_SEARCH,
    "wechat": _DATATAP_WECHAT_SEARCH,
}
_ZHEJIANG_LOCATION_TERMS = ("浙江", "湖州")
_MAX_CANDIDATES_PER_PLATFORM = 10
_DATATAP_USER_PROFILE = "datatap.insight.social.statistic.user.profile.v1"
_DATATAP_KOL_DETAIL = "datatap.social.grow.kol.detail.v1"
_PROFILE_MEDIA_LABELS = {
    "xiaohongshu": "小红书",
    "douyin": "抖音",
    "weibo": "微博",
}
_FALLBACK_BRAND_TOOLS = (
    "datatap.insight.query.analysis.v1",
    "datatap.insight.social.statistic.trend.v1",
    "datatap.insight.social.statistic.overview.v1",
    "datatap.insight.social.statistic.hot.topic.v1",
)


class Planner:
    def __init__(self, *, model: ModelAdapter) -> None:
        self._model = model

    async def plan(self, context: PlannerContext) -> ToolPlan:
        try:
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
                    max_tokens=8192,
                )
            )
            sanitized = self._sanitize_plan_arguments(result.value, context)
            compiled = self._compile_supported_search_defaults(sanitized, context)
            plan = compiled.model_copy(update={"analysis_scope": derive_analysis_scope(compiled)})
            self._validate(plan, context)
            return plan
        except (ModelAdapterError, PlanValidationError):
            # The router is deliberately isolated to this failure path.  It
            # cannot override a successful model plan, but it keeps the task
            # executable when the planner provider is unavailable.
            return self._fallback_plan(context)

    @staticmethod
    def _fallback_plan(context: PlannerContext) -> ToolPlan:
        text = "\n".join(message.content for message in context.recent_messages)
        routing = classify_analysis_request(text, context.brief)
        tools = {tool.internal_name: tool for tool in context.tools}
        steps: list[ToolPlanStep] = []
        next_id = 1

        if routing.scope in {"brand", "hybrid"}:
            for internal_name in _FALLBACK_BRAND_TOOLS:
                tool = tools.get(internal_name)
                if tool is None:
                    continue
                arguments = Planner._fallback_arguments(tool, context)
                try:
                    validate_input(arguments, tool.input_schema)
                except McpValidationError:
                    continue
                steps.append(
                    ToolPlanStep(
                        id=f"step_{next_id}",
                        internal_tool_name=internal_name,
                        arguments=arguments,
                        evidence_kind="brand",
                        evidence_goal="品牌指标真实数据",
                    )
                )
                next_id += 1
                break

        for platform in context.brief.platforms:
            internal_name = _DATATAP_SEARCH_BY_PLATFORM.get(platform)
            if internal_name is None or internal_name not in tools:
                continue
            tool = tools[internal_name]
            arguments = Planner._fallback_arguments(tool, context)
            try:
                validate_input(arguments, tool.input_schema)
            except McpValidationError:
                continue
            steps.append(
                ToolPlanStep(
                    id=f"step_{next_id}",
                    internal_tool_name=internal_name,
                    arguments=arguments,
                    evidence_kind="kol",
                    evidence_goal=f"{platform} 匹配相关 KOL",
                )
            )
            next_id += 1
            if len(steps) >= 10:
                break

        if not any(step.evidence_kind == "kol" for step in steps):
            raise PlanValidationError("FALLBACK_NO_KOL_TOOL")
        fallback = ToolPlan(
            objective="；".join(routing.objectives) or "KOL 匹配分析",
            primary_intent=routing.scope,
            objectives=routing.objectives,
            steps=tuple(steps),
        )
        compiled = Planner._compile_supported_search_defaults(fallback, context)
        plan = compiled.model_copy(update={"analysis_scope": derive_analysis_scope(compiled)})
        Planner._validate(plan, context)
        return plan

    @staticmethod
    def _fallback_arguments(tool: Any, context: PlannerContext) -> dict[str, Any]:
        schema = tool.input_schema if isinstance(tool.input_schema, dict) else {}
        properties = schema.get("properties", {}) if isinstance(schema.get("properties"), dict) else {}
        arguments: dict[str, Any] = {}
        for name in schema.get("required", []) if isinstance(schema.get("required"), list) else ():
            child = properties.get(name, {})
            if name == "request":
                arguments[name] = {"page": 1, "size": _MAX_CANDIDATES_PER_PLATFORM}
            elif child.get("type") == "string":
                arguments[name] = context.brief.brand or context.brief.category
            elif child.get("type") == "integer":
                arguments[name] = 1
            elif child.get("type") == "number":
                arguments[name] = 0
            elif child.get("type") == "array":
                arguments[name] = []
        return arguments

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
                max_tokens=8192,
            )
        )
        sanitized = _expand_user_profile_media(
            self._sanitize_plan_arguments(result.value, context), context
        )
        plan = sanitized.model_copy(update={"analysis_scope": derive_analysis_scope(sanitized)})
        if len(plan.steps) > recovery.remaining_calls:
            raise PlanValidationError("REPLAN_CALL_BUDGET_EXCEEDED")
        prohibited_ids = set(recovery.completed_step_ids) | {
            failure.step_id for failure in recovery.failed_steps
        }
        if any(step.id in prohibited_ids for step in plan.steps):
            raise PlanValidationError("REPLAN_REUSES_COMPLETED_STEP")
        self._validate(plan, context, completed_evidence_kinds=recovery.completed_evidence_kinds)
        return plan

    @staticmethod
    def _sanitize_plan_arguments(plan: ToolPlan, context: PlannerContext) -> ToolPlan:
        """Drop undeclared model arguments before strict schema validation.

        The provider sometimes adds helpful-looking fields (for example
        ``metrics``) that are not part of the reviewed MCP contract.  Keeping
        the strict validator is important, but an undeclared optional field
        must not make an otherwise usable plan fail.  Required fields and all
        declared values remain subject to the normal JSON Schema validator.
        """
        tools = {tool.internal_name: tool for tool in context.tools}
        steps = tuple(
            step.model_copy(
                update={
                    "arguments": _prune_arguments(
                        step.arguments,
                        (tools[step.internal_tool_name].input_schema
                         if step.internal_tool_name in tools
                         else {}),
                    )
                }
            )
            for step in plan.steps
        )
        return plan.model_copy(update={"steps": steps})

    @staticmethod
    def _compile_supported_search_defaults(plan: ToolPlan, context: PlannerContext) -> ToolPlan:
        """补齐已审核工具能稳定表达、且来自用户输入的筛选条件。

        这不是替模型补造数据：它只将会话中明确出现的品牌、近 30 天活跃
        和湖州/浙江地区诉求，规范化为 DataTap 小红书工具实际支持的字段。
        """
        plan = _expand_user_profile_media(plan, context)
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
                        "evidence_kind": "kol",
                    }
                )
            )
            existing_tools.add(internal_name)
            next_step_number += 1
            changed = True
        # 有搜索步骤但没有详情步骤时自动补一个（空 uid 占位，执行器回填；
        # 搜索无结果时执行器跳过不计费），保证候选达人能拿到详情与受众画像。
        search_step_ids = tuple(
            step.id
            for step in compiled_steps
            if step.internal_tool_name in _DATATAP_SEARCH_BY_PLATFORM.values()
        )
        has_detail_step = any(
            step.internal_tool_name == _DATATAP_KOL_DETAIL for step in compiled_steps
        )
        detail_tool_available = _DATATAP_KOL_DETAIL in available_tools
        if search_step_ids and not has_detail_step and detail_tool_available and len(compiled_steps) < 10:
            while any(step.id == f"step_{next_step_number}" for step in compiled_steps):
                next_step_number += 1
            detail_platform = (
                context.brief.platforms[0] if context.brief.platforms else "douyin"
            )
            compiled_steps.append(
                plan.steps[0].model_copy(
                    update={
                        "id": f"step_{next_step_number}",
                        "internal_tool_name": _DATATAP_KOL_DETAIL,
                        "arguments": {
                            "platform": detail_platform,
                            "kwUidList": [],
                            "scope": ["accountTrend", "fansAudience", "postSummaryStatistics"],
                        },
                        "depends_on": search_step_ids,
                        "evidence_goal": "候选达人详情与受众画像",
                        "evidence_kind": "kol",
                    }
                )
            )
            changed = True
        return plan.model_copy(update={"steps": tuple(compiled_steps)}) if changed else plan

    @staticmethod
    def _validate(
        plan: ToolPlan,
        context: PlannerContext,
        *,
        completed_evidence_kinds: tuple[str, ...] = (),
    ) -> None:
        if len(plan.steps) > 10:
            raise PlanValidationError("TOO_MANY_TOOL_CALLS")
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
        evidence_kinds = {step.evidence_kind for step in plan.steps}
        evidence_kinds.update(completed_evidence_kinds)
        # Product invariant: every task returns a matching KOL dimension.
        if "kol" not in evidence_kinds:
            raise PlanValidationError("EVIDENCE_SCOPE_NOT_COVERED")
        if plan.primary_intent in {"brand", "hybrid"} and "brand" not in evidence_kinds:
            raise PlanValidationError("EVIDENCE_SCOPE_NOT_COVERED")
        build_execution_batches(plan)


__all__ = ["PlanValidationError", "Planner"]


def _expand_user_profile_media(plan: ToolPlan, context: PlannerContext) -> ToolPlan:
    """将画像工具的媒体参数编译为用户选中的单个平台调用。

    DataTap 的 user.profile 工具的 ``media`` 是单值枚举，不接受模型常见的
    “依次为小红书、抖音”占位字符串。一个工具步骤也不能携带多个媒体，
    因此按会话平台拆成多个步骤，保证每个 MCP 调用都能通过 Schema 校验。
    """
    profile_steps = [
        step for step in plan.steps if step.internal_tool_name == _DATATAP_USER_PROFILE
    ]
    if not profile_steps:
        return plan

    desired_media = [
        _PROFILE_MEDIA_LABELS[platform]
        for platform in context.brief.platforms
        if platform in _PROFILE_MEDIA_LABELS
    ]
    if not desired_media:
        return plan

    expanded: list[tuple[str, ToolPlanStep]] = []
    emitted_media: set[str] = set()
    for step in plan.steps:
        if step.internal_tool_name != _DATATAP_USER_PROFILE:
            expanded.append((step.id, step))
            continue

        # Existing profile steps are consolidated against the selected media;
        # this prevents a model-generated duplicate from consuming extra calls.
        media_for_step = [media for media in desired_media if media not in emitted_media]
        if not media_for_step:
            continue
        for index, media in enumerate(media_for_step):
            temporary_id = step.id if index == 0 else f"{step.id}_media_{index}"
            arguments = dict(step.arguments)
            arguments["media"] = media
            expanded_step = step.model_copy(update={"id": temporary_id, "arguments": arguments})
            expanded.append((step.id, expanded_step))
            emitted_media.add(media)

    # Reassign IDs after expansion and fan out dependencies to all generated
    # media-specific steps. This keeps the persisted plan graph valid.
    id_map: dict[str, tuple[str, ...]] = {}
    for index, (original_id, _step) in enumerate(expanded, start=1):
        id_map.setdefault(original_id, ())
        id_map[original_id] = (*id_map[original_id], f"step_{index}")
    normalized_steps: list[ToolPlanStep] = []
    for index, (original_id, step) in enumerate(expanded, start=1):
        dependencies = tuple(
            dependency_id
            for dependency in step.depends_on
            for dependency_id in id_map.get(dependency, (dependency,))
        )
        normalized_steps.append(
            step.model_copy(update={"id": f"step_{index}", "depends_on": dependencies})
        )
    return plan.model_copy(update={"steps": tuple(normalized_steps)})


def _prune_arguments(value: object, schema: dict) -> object:
    """Recursively remove fields rejected by closed object schemas."""
    if isinstance(value, list):
        item_schema = schema.get("items") if isinstance(schema, dict) else None
        return [_prune_arguments(item, item_schema if isinstance(item_schema, dict) else {}) for item in value]
    if not isinstance(value, dict) or not isinstance(schema, dict):
        return value

    variants = [schema]
    for key in ("anyOf", "oneOf", "allOf"):
        if isinstance(schema.get(key), list):
            variants.extend(item for item in schema[key] if isinstance(item, dict))
    properties: dict[str, dict] = {}
    for variant in variants:
        raw_properties = variant.get("properties")
        if isinstance(raw_properties, dict):
            properties.update(
                {name: child for name, child in raw_properties.items() if isinstance(child, dict)}
            )
    additional = schema.get("additionalProperties")
    if additional is False or properties:
        result: dict[str, object] = {}
        for key, item in value.items():
            child_schema = properties.get(key)
            if child_schema is not None:
                result[key] = _prune_arguments(item, child_schema)
            elif additional is not False:
                result[key] = _prune_arguments(item, additional if isinstance(additional, dict) else {})
        return result
    return {key: _prune_arguments(item, {}) for key, item in value.items()}
