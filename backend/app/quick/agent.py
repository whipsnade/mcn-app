"""快捷功能（爆贴/达人推荐/达人详情）的模型驱动同步小循环。

代码只组装场景 prompt 并保留护栏：工具白名单/Schema 校验（resolve_agent_call
同款）、计费执行（调用方注入的 call 走 QuickCallService）、轮次上限与无效
决策回喂。选哪个工具、填什么参数、什么时候结束全部由模型决策。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp_gateway.transport import JsonValue
from app.model.contracts import ChatMessage, ModelAdapter, StructuredModelRequest
from app.model.exemplars import find_success_exemplars
from app.model.prompts import QUICK_AGENT_PROMPT
from app.orchestration.loop import (
    AgentDecision,
    AgentLoopContext,
    EvidenceNote,
    resolve_agent_call,
)
from app.orchestration.schemas import PlanValidationError, PlannerTool
from app.quick.errors import QuickCallFailedError
from app.reporting.analysis_reports import sanitize_evidence
from app.model.persona import describe_user_persona


# 小循环护栏：最多 8 轮工具调用决策，第 9 轮起强制 finish；连续 2 次无效
# 决策（白名单/Schema 校验失败或 finish 结果不合契约）直接报错。
QUICK_AGENT_MAX_ROUNDS = 8
QUICK_AGENT_MAX_INVALID_STREAK = 2

MATCH_BEST_TAG_TOOL = "datatap.insight.match.best.tag.v1"
RAW_POSTS_TOOL = "datatap.insight.query.raw.posts.v1"
KOL_DETAIL_TOOL = "datatap.social.grow.kol.detail.v1"
KOL_MATCH_MENTIONS_TAG_TOOL = "datatap.social.grow.kol.match.mentions.tag.v1"

# 内容选题（social-grow-content-mcp）热词链路：字典 → 热词榜 → 热词下钻帖子。
# 当前仅有小红书平台工具。
HOTWORDS_XHS_TOOLS = (
    "datatap.content.hotwords.xiaohongshu.dictionary.v1",
    "datatap.content.hotwords.xiaohongshu.list.v1",
    "datatap.content.hotwords.xiaohongshu.posts.v1",
)

KOL_SEARCH_TOOLS = {
    "xiaohongshu": "datatap.xiaohongshu.kol.search.v1",
    "douyin": "datatap.douyin.kol.search.v1",
    "bilibili": "datatap.social.grow.kol.bilibili.search.v1",
    "weibo": "datatap.social.grow.kol.weibo.search.v1",
    "wechat": "datatap.social.grow.kol.wechat.search.v1",
}

# 社媒统计/原帖工具的 datasource 规范取值（与 AGENT_LOOP 提示词一致）。
DATASOURCE_BY_PLATFORM = {
    "xiaohongshu": "小红书",
    "douyin": "短视频__抖音",
    "weibo": "微博",
    "wechat": "微信",
    "bilibili": "视频__哔哩哔哩",
}

# 快捷渠道的渠道权限集合：与 agent 循环一致，覆盖全部五个社媒渠道。
_QUICK_CHANNELS = ("xiaohongshu", "douyin", "bilibili", "weibo", "wechat")

_OUTPUT_CONTRACTS = {
    "top_posts": "result 必须是帖子对象列表（保留上游原始字段），按互动数倒序至多 10 条",
    "kol_recommend": (
        "result 必须是达人对象列表（保留上游原始字段，含报价与平台字段），覆盖请求的全部平台"
    ),
    "kol_detail": (
        'result 必须是 {"detail": 达人详情对象, "posts": 帖子对象列表}；'
        '热帖获取失败时 posts 给空列表并附加 "posts_degraded": true'
    ),
    "campaign_evaluate": (
        'result 必须是 {"title": 评估标题（不超过 20 个字）, "analysis_markdown": 评估结论'
        " Markdown（非空）}；结论只能基于本轮已获得的工具证据"
    ),
}

_LIST_ADAPTER = TypeAdapter(list)

# call(internal_tool_name, arguments) -> 解析后的 DataTap 载荷（计费/留痕在实现内）。
QuickToolCaller = Callable[[str, dict[str, Any]], Awaitable[JsonValue]]


class QuickDecision(BaseModel):
    """quick_feature 模型的单轮决策输出。"""

    model_config = ConfigDict(extra="forbid")

    action: Literal["call_tool", "finish"]
    internal_tool_name: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: Any = None
    rationale: str = Field(default="", max_length=500)


class KolDetailFeatureResult(BaseModel):
    """达人详情的 finish 结果契约。"""

    detail: dict[str, Any]
    posts: list[Any] = Field(default_factory=list)
    posts_degraded: bool = False


class CampaignEvaluateFeatureResult(BaseModel):
    """活动评估的 finish 结果契约（对象型）。"""

    title: str = Field(min_length=1, max_length=20)
    analysis_markdown: str = Field(min_length=1)


def quick_feature_tool_names(feature: str, platforms: tuple[str, ...]) -> tuple[str, ...]:
    """按 feature 给出的可用工具子集（内部名）。"""
    if feature == "top_posts":
        names: list[str] = [MATCH_BEST_TAG_TOOL, RAW_POSTS_TOOL]
        # 热词链路目前只有小红书工具，仅在小红书爆贴场景提供。
        if "xiaohongshu" in platforms:
            names.extend(HOTWORDS_XHS_TOOLS)
        return tuple(names)
    if feature == "kol_detail":
        return (KOL_DETAIL_TOOL, RAW_POSTS_TOOL)
    if feature == "kol_recommend":
        names = [KOL_MATCH_MENTIONS_TAG_TOOL]
        names.extend(KOL_SEARCH_TOOLS[platform] for platform in platforms if platform in KOL_SEARCH_TOOLS)
        return tuple(names)
    if feature == "campaign_evaluate":
        # 逐个达人查证：五平台 KOL 搜索 + kol_detail 补字段 + 标签匹配。
        names = [KOL_MATCH_MENTIONS_TAG_TOOL, KOL_DETAIL_TOOL]
        names.extend(KOL_SEARCH_TOOLS[platform] for platform in platforms if platform in KOL_SEARCH_TOOLS)
        return tuple(names)
    raise QuickCallFailedError("unknown_feature")


def validate_feature_result(feature: str, result: Any) -> Any:
    """finish 结果的结构校验；不合契约抛 PlanValidationError（回喂或报错）。

    爆贴/达人推荐 = 列表（模型常包一层 {"items": [...]}，单列表值时自动解包）；
    达人详情 = {"detail": 对象, "posts": 列表}；活动评估 = {"title", "analysis_markdown"}。
    """
    if feature in ("top_posts", "kol_recommend"):
        rows = result
        if isinstance(rows, dict):
            lists = [value for value in rows.values() if isinstance(value, list)]
            rows = lists[0] if len(lists) == 1 else None
        try:
            return _LIST_ADAPTER.validate_python(rows)
        except ValidationError as error:
            raise PlanValidationError("QUICK_RESULT_INVALID") from error
    if feature == "campaign_evaluate":
        try:
            return CampaignEvaluateFeatureResult.model_validate(result)
        except ValidationError as error:
            raise PlanValidationError("QUICK_RESULT_INVALID") from error
    try:
        return KolDetailFeatureResult.model_validate(result)
    except ValidationError as error:
        raise PlanValidationError("QUICK_RESULT_INVALID") from error


def _default_period(days: int = 29) -> dict[str, Any]:
    today = date.today()
    start = today - timedelta(days=days)
    return {
        "unit": "day",
        "value": days,
        "start": start.isoformat(),
        "end": today.isoformat(),
    }


def _user_content(
    *,
    feature: str,
    goal: str,
    scenario: dict[str, Any],
    industries: list[str],
    tools: tuple[PlannerTool, ...],
    notes: list[EvidenceNote],
    exemplars: list[dict[str, Any]],
    force_finish: bool,
) -> str:
    return json.dumps(
        {
            "feature": feature,
            "goal": goal,
            "industries": industries,
            "user_persona": describe_user_persona(industries),
            "scenario": scenario,
            "tools": [tool.model_dump(mode="json") for tool in tools],
            "evidence": [note.model_dump(mode="json") for note in notes],
            "exemplars": exemplars,
            "output_contract": _OUTPUT_CONTRACTS[feature],
            "current_date": date.today().isoformat(),
            "force_finish": force_finish,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


async def run_quick_feature(
    *,
    db: AsyncSession,
    model: ModelAdapter,
    call: QuickToolCaller,
    tools: tuple[PlannerTool, ...],
    user_id: str,
    feature: str,
    goal: str,
    scenario: dict[str, Any],
    industries: list[str],
    tags: list[str],
    period_days: int = 29,
    system_prompt: str | None = None,
) -> Any:
    """同步小循环：模型决策 → 校验 → 计费执行 → 证据回填，直至 finish。

    返回通过该 feature 输出契约校验的原始结果（端点层再做归一化清洗）。
    period_days 为场景的默认时间窗（爆贴用 7 日热榜传 6）。
    system_prompt 缺省为 QUICK_AGENT_PROMPT.system（活动评估等场景传专用 prompt）。
    """
    if not tools:
        raise QuickCallFailedError("tool_not_enabled")
    context = AgentLoopContext(
        recent_messages=(),
        tools=tools,
        allowed_channels=_QUICK_CHANNELS,
        requested_period=_default_period(period_days),
    )
    log_context = {"user_id": user_id, "tags": list(tags)}
    exemplars = await find_success_exemplars(db, purpose="quick_feature", tags=tags)
    notes: list[EvidenceNote] = []
    invalid_streak = 0
    for round_index in range(QUICK_AGENT_MAX_ROUNDS + 1):
        # 超出 8 轮：把现有证据交给模型，要求立即 finish。
        force_finish = round_index >= QUICK_AGENT_MAX_ROUNDS
        result = await model.complete_json(
            StructuredModelRequest(
                purpose="quick_feature",
                template_name=QUICK_AGENT_PROMPT.name,
                messages=(
                    ChatMessage(role="system", content=system_prompt or QUICK_AGENT_PROMPT.system),
                    ChatMessage(
                        role="user",
                        content=_user_content(
                            feature=feature,
                            goal=goal,
                            scenario=scenario,
                            industries=industries,
                            tools=tools,
                            notes=notes,
                            exemplars=exemplars,
                            force_finish=force_finish,
                        ),
                    ),
                ),
                output_model=QuickDecision,
                max_tokens=4096,
                log_context=log_context,
            )
        )
        decision = result.value
        if decision.action == "finish":
            try:
                return validate_feature_result(feature, decision.result)
            except PlanValidationError:
                invalid_streak += 1
                if invalid_streak >= QUICK_AGENT_MAX_INVALID_STREAK:
                    raise QuickCallFailedError("invalid_result") from None
                notes.append(
                    EvidenceNote(
                        step_id=f"invalid_result_{invalid_streak}",
                        tool="output_contract",
                        status="failed",
                        summary=(
                            "上一次 finish 的 result 不满足输出契约（"
                            + _OUTPUT_CONTRACTS[feature]
                            + "），请按契约重新整理已获证据后 finish。"
                        ),
                    )
                )
                continue
        if force_finish:
            raise QuickCallFailedError("round_limit")
        try:
            agent_decision = AgentDecision(
                action="call_tool",
                internal_tool_name=decision.internal_tool_name,
                arguments=decision.arguments,
            )
            tool, arguments = resolve_agent_call(agent_decision, context)
        except (PlanValidationError, ValidationError) as error:
            invalid_streak += 1
            if invalid_streak >= QUICK_AGENT_MAX_INVALID_STREAK:
                raise QuickCallFailedError("invalid_decision") from error
            code = error.code if isinstance(error, PlanValidationError) else "INVALID_TOOL_ARGUMENTS"
            notes.append(
                EvidenceNote(
                    step_id=f"invalid_{invalid_streak}",
                    tool=decision.internal_tool_name or "unknown",
                    status="failed",
                    summary=(
                        f"上一次决策未通过校验（{code}），"
                        "请在 tools 列表内选择工具并按其 input_schema 填写参数。"
                    ),
                )
            )
            continue
        invalid_streak = 0
        step_id = f"call_{round_index + 1}"
        try:
            payload = await call(tool.internal_name, arguments)
        except QuickCallFailedError as error:
            # 调用失败（预留已释放，不计费）：回喂给模型自行换工具/参数或 finish。
            notes.append(
                EvidenceNote(
                    step_id=step_id,
                    tool=tool.internal_name,
                    status="failed",
                    summary=(
                        f"调用失败（{error.error_type}）；"
                        "可换参数重试、改用其他工具，或在现有证据足够时 finish。"
                    ),
                )
            )
            continue
        notes.append(
            EvidenceNote(
                step_id=step_id,
                tool=tool.internal_name,
                status="settled",
                summary=sanitize_evidence(slim_quick_evidence(payload)),
            )
        )
    raise QuickCallFailedError("round_limit")


# 列表类载荷（帖子/达人清单）逐行保留的字段：模型要把这些行整理进 finish
# result，必须先拿到完整可解析的行；长文本字段（内容正文）对结果呈现无用，
# 直接剔除以控制 prompt 体积。
_SLIM_ROW_KEYS = frozenset({
    "唯一ID", "内容ID", "平台", "站点", "标题", "发布时间", "帖子链接",
    "互动数", "阅读数", "评论数", "收藏数", "点赞数", "转发数", "用户昵称", "用户ID",
    "账号ID (kwUid)", "昵称", "粉丝数", "抖音粉丝数", "平均互动", "综合评分",
    "互动率-图文笔记", "互动率-日常作品", "有效粉丝率", "爆文率",
    "城市", "IP属地", "官方报价", "预估报价",
    "Grow-博主类目标签", "Grow-达人类型标签",
})


def _slim_row(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    slimmed = {key: value for key, value in item.items() if key in _SLIM_ROW_KEYS}
    return slimmed or item


def slim_quick_evidence(payload: Any) -> Any:
    """把清单类载荷裁剪为结构完整的小 JSON。

    sanitize_evidence 超过 6000 字符会硬截断，产出的是半个 JSON——
    模型无法解析就只能 finish 出空结果（真实案例）。先按字段白名单
    瘦身，再交给 sanitize_evidence 兜底脱敏。
    """
    if isinstance(payload, list):
        return [_slim_row(item) for item in payload[:30]]
    if isinstance(payload, dict):
        return {
            key: ([_slim_row(item) for item in value[:30]] if isinstance(value, list) else value)
            for key, value in payload.items()
        }
    return payload


__all__ = [
    "DATASOURCE_BY_PLATFORM",
    "KOL_SEARCH_TOOLS",
    "CampaignEvaluateFeatureResult",
    "KolDetailFeatureResult",
    "QUICK_AGENT_MAX_ROUNDS",
    "QuickDecision",
    "quick_feature_tool_names",
    "run_quick_feature",
    "slim_quick_evidence",
    "validate_feature_result",
]
