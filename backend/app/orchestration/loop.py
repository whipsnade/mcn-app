"""迭代式 agent 循环的上下文、决策契约与轨迹持久化。

agent 任务（kind="agent"）不使用一次性 ToolPlan：模型每轮根据已获证据
决定调用一个工具或结束。轨迹（已确定的步骤与结果）持久化在任务的
``plan_json`` 中，恢复时按原样重放未完成的步骤，保证 ``arguments_loader``
能取回与 ``mcp_calls.arguments_digest`` 一致的参数。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.mcp_gateway.contracts import DataTapService
from app.mcp_gateway.validation import McpValidationError, validate_input
from app.orchestration.schemas import PlanValidationError, PlannerMessage, PlannerTool


TRAJECTORY_SCHEMA = "agent_trajectory_v1"

# 各 DataTap 服务实际覆盖的社媒渠道，用于渠道权限校验。
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

# DataTap insight 统计工具的 datasource 规范取值（platform__source 格式）。
# 已向上游核实：平台集合为 小红书/短视频/微博/微信/视频/电商/博客/问答/新闻/论坛，
# 视频平台站点为 哔哩哔哩/腾讯视频/爱奇艺(视频)/搜狐(视频)，短视频平台站点为
# 抖音/快手/微信视频号/今日头条。模型常写 douyin/bilibili/B站 等别名，
# 上游校验直接拒绝，这里在调用前归一化，避免白烧一次调用预算。
_DATASOURCE_ALIASES = {
    "xiaohongshu": "小红书",
    "red": "小红书",
    "douyin": "短视频__抖音",
    "抖音": "短视频__抖音",
    "tiktok": "短视频__抖音",
    "短视频__douyin": "短视频__抖音",
    "weibo": "微博",
    "wechat": "微信",
    "bilibili": "视频__哔哩哔哩",
    "b站": "视频__哔哩哔哩",
    "视频__bilibili": "视频__哔哩哔哩",
    "视频__b站": "视频__哔哩哔哩",
}

_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d")
# 上游硬限制为 366 天，留一天余量。
_MAX_RANGE_DAYS = 365


def normalize_agent_arguments(
    arguments: dict[str, Any],
    *,
    tool: PlannerTool | None = None,
    default_period: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把模型生成的参数规范化为 DataTap 实际接受的取值。

    只做确定性映射（别名→规范名、默认时间窗回填、时间窗钳制、按工具必填
    规则补齐 name），不补造任何数据；无法识别的取值原样保留，交给上游
    报错并回喂给模型。
    """
    normalized = dict(arguments)
    datasource = normalized.get("datasource")
    if isinstance(datasource, list):
        mapped: list[str] = []
        for item in datasource:
            value = str(item).strip()
            if not value:
                continue
            canonical = _DATASOURCE_ALIASES.get(value.casefold()) or _DATASOURCE_ALIASES.get(value)
            mapped.append(canonical or value)
        normalized["datasource"] = list(dict.fromkeys(mapped))
    if tool is not None and default_period:
        # 会话未明确时间范围时默认最近三个月（见 routing._period_from_text）。
        properties = (
            tool.input_schema.get("properties", {})
            if isinstance(tool.input_schema, dict)
            else {}
        )
        for field, key in (("start_time", "start"), ("end_time", "end")):
            if field in properties and not normalized.get(field) and default_period.get(key):
                normalized[field] = str(default_period[key])
    if normalized.get("target_type") == "keyword" and not normalized.get("name"):
        anys = normalized.get("anys")
        if (
            isinstance(anys, list)
            and anys
            and isinstance(anys[0], list)
            and anys[0]
        ):
            # keyword 类型 name 必填（圈选名称）：用首组关键词补齐。
            normalized["name"] = str(anys[0][0])
    return _clamp_time_range(normalized)


def _clamp_time_range(arguments: dict[str, Any]) -> dict[str, Any]:
    start_raw = arguments.get("start_time")
    end_raw = arguments.get("end_time")
    if not isinstance(start_raw, str) or not isinstance(end_raw, str):
        return arguments
    start, start_format = _parse_time(start_raw.strip())
    end, _ = _parse_time(end_raw.strip())
    if start is None or end is None or (end - start).days <= _MAX_RANGE_DAYS:
        return arguments
    clamped = dict(arguments)
    clamped["start_time"] = (end - timedelta(days=_MAX_RANGE_DAYS)).strftime(start_format)
    return clamped


def _parse_time(value: str) -> tuple[datetime | None, str]:
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(value, fmt), fmt
        except ValueError:
            continue
    return None, _TIME_FORMATS[0]


class EvidenceNote(BaseModel):
    """一轮工具调用的脱敏结果摘要，回填进下一轮模型上下文。"""

    model_config = ConfigDict(extra="forbid")

    step_id: str = Field(min_length=1, max_length=32)
    tool: str = Field(min_length=1, max_length=128)
    status: Literal["settled", "failed"]
    summary: Any = None


class AgentLoopContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recent_messages: tuple[PlannerMessage, ...]
    tools: tuple[PlannerTool, ...]
    allowed_channels: tuple[str, ...]
    notes: tuple[EvidenceNote, ...] = ()
    # 服务端注入的真实日期与解析出的时间窗：模型自身的时间感知不可靠，
    # 曾因此把统计查询落到一年上限之外而拿到空数据。
    current_date: str = ""
    requested_period: dict[str, Any] = Field(default_factory=dict)
    # brainstorm 澄清确认的参数画像（brand/category/platforms/goal 等），
    # 优先级高于从消息文本推断；空 dict 表示会话未经过澄清。
    param_profile: dict[str, Any] = Field(default_factory=dict)
    # 用户行业画像描述（来自 users.industries 的业务视角翻译），供模型理解
    # "用户是谁"并自主选择贴合的工具路径；空字符串表示无画像。
    user_persona: str = ""
    # KOL 圈选 Excel 导出字段契约（selection/contract.py 的 ExportFieldContract
    # 序列化结果）：required_field_names/labels/notes 随会话画像动态生成，
    # 告诉模型需要为每位达人采齐哪些导出字段；空 dict 表示无契约。
    export_contract: dict[str, Any] = Field(default_factory=dict)
    # prompt 学习日志上下文（user_id/session_id/task_id/tags）；仅用于落库，
    # exclude=True 保证不进入发给模型的 prompt JSON。
    log_context: dict[str, Any] = Field(default_factory=dict, exclude=True)


class AgentDecision(BaseModel):
    """agent_loop 模型的单轮决策输出。"""

    model_config = ConfigDict(extra="forbid")

    action: Literal["call_tool", "finish"]
    internal_tool_name: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    evidence_goal: str = Field(default="", max_length=300)
    rationale: str = Field(default="", max_length=500)
    # finish 时面向用户的圈选结论，直接写入 assistant 消息；为空时服务端回退固定文案。
    conclusion: str = Field(default="", max_length=2000)


class TrajectoryStep(BaseModel):
    """已确定要执行的步骤；参数必须与 mcp_calls 记录保持逐字节一致。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^step_[0-9]+$")
    internal_tool_name: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any]
    evidence_goal: str = Field(default="", max_length=300)


class AgentTrajectory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_: str = Field(default=TRAJECTORY_SCHEMA, alias="schema")
    steps: list[TrajectoryStep] = Field(default_factory=list)
    results: list[EvidenceNote] = Field(default_factory=list)

    def as_plan_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


def restore_agent_trajectory(plan_json: dict[str, Any] | None) -> AgentTrajectory:
    if isinstance(plan_json, dict) and plan_json.get("schema") == TRAJECTORY_SCHEMA:
        return AgentTrajectory.model_validate(plan_json)
    return AgentTrajectory()


def resolve_agent_call(
    decision: AgentDecision, context: AgentLoopContext
) -> tuple[PlannerTool, dict[str, Any]]:
    """解析单步调用：工具/渠道校验 → 参数归一化 → 封闭 Schema 校验。

    归一化在校验前完成，缺省起止时间按上下文时间窗（默认最近三个月）
    回填后再走必填校验。
    """
    if not decision.internal_tool_name:
        raise PlanValidationError("AGENT_TOOL_MISSING")
    tools = {tool.internal_name: tool for tool in context.tools}
    tool = tools.get(decision.internal_tool_name)
    if tool is None:
        raise PlanValidationError("TOOL_NOT_ALLOWED")
    supported_channels = _SERVICE_CHANNELS.get(tool.service, frozenset())
    if not supported_channels.intersection(context.allowed_channels):
        raise PlanValidationError("SERVICE_CHANNEL_NOT_ALLOWED")
    arguments = normalize_agent_arguments(
        decision.arguments, tool=tool, default_period=context.requested_period
    )
    # 模型附加的未声明字段（如 metrics）先剔除，不能让一个无害的多余字段
    # 毁掉整个可用的调用。
    arguments = _prune_arguments(arguments, tool.input_schema)
    if not isinstance(arguments, dict):
        raise PlanValidationError("INVALID_TOOL_ARGUMENTS")
    try:
        validate_input(arguments, tool.input_schema)
    except McpValidationError as exc:
        raise PlanValidationError("INVALID_TOOL_ARGUMENTS") from exc
    return tool, arguments


def validate_agent_decision(decision: AgentDecision, context: AgentLoopContext) -> PlannerTool:
    """工具/渠道/参数校验（单步版本），返回匹配到的已审核工具。"""
    tool, _arguments = resolve_agent_call(decision, context)
    return tool


__all__ = [
    "TRAJECTORY_SCHEMA",
    "AgentDecision",
    "AgentLoopContext",
    "AgentTrajectory",
    "EvidenceNote",
    "TrajectoryStep",
    "normalize_agent_arguments",
    "resolve_agent_call",
    "restore_agent_trajectory",
    "validate_agent_decision",
]
