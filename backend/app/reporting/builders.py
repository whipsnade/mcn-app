"""品牌/活动报告构建器：轨迹证据聚合 → 模型撰写 ReportDocument → 会话级落库。

参照 run_kol_analysis 的事务模式：证据先从 task.plan_json 读出（纯内存），
模型调用不持有数据库事务，落库走 build_session_report（按 report_type 独立编号）。

品牌路径为 v2（Task 6）：assemble_brand_report 代码组装结构化快照 →
build_brand_narrative 模型撰写叙事 → 快照 + 兼容 ReportDocument 一次落库
（payload_json + template_version="brand_report_v2"）；活动路径保持
campaign_analysis_v1 模型直出 ReportDocument。
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.model.contracts import (
    ChatMessage,
    ModelAdapter,
    StructuredModelRequest,
    ThinkingSink,
)
from app.model.prompts import CAMPAIGN_ANALYSIS_PROMPT, PromptTemplate
from app.reporting.analysis_reports import AnalysisReportService, sanitize_evidence
from app.reporting.blocks import (
    ChartBlock,
    ChartSeries,
    MarkdownBlock,
    MetricGridBlock,
    MetricItem,
    ReportBlock,
    ReportDocument,
    SourceItem,
    SourcesBlock,
    TableBlock,
)
from app.reporting.brand_assembler import assemble_brand_report
from app.reporting.brand_narrative import build_brand_narrative
from app.reporting.brand_payload import BrandReportPayload
from app.reporting.models import AnalysisReport


def collect_goal_evidence(
    task_plan_json: dict[str, Any] | None, goal_id: str | None = None
) -> list[dict[str, Any]]:
    """从 plan_json 提取 settled 证据并脱敏截断。

    返回 [{"tool": 内部工具名, "structured_content": 脱敏后的证据}]；
    无轨迹或无 settled 证据返回 []。v2 轨迹（agent_trajectory_v2，按 goal
    分片）按 goal_id 取对应切片（切片缺失返回 []），goal_id 为 None 时合并
    所有切片；v1 轨迹忽略 goal_id。
    """
    if not isinstance(task_plan_json, dict):
        return []
    if task_plan_json.get("schema") == "agent_trajectory_v2":
        goals = task_plan_json.get("goals")
        if not isinstance(goals, dict):
            return []
        if goal_id is not None:
            goal_slice = goals.get(goal_id)
            if not isinstance(goal_slice, dict):
                return []
            return _settled_evidence(goal_slice.get("results"))
        evidence: list[dict[str, Any]] = []
        for goal_slice in goals.values():
            if isinstance(goal_slice, dict):
                evidence.extend(_settled_evidence(goal_slice.get("results")))
        return evidence
    return _settled_evidence(task_plan_json.get("results"))


def _settled_evidence(results: Any) -> list[dict[str, Any]]:
    if not isinstance(results, list):
        return []
    evidence: list[dict[str, Any]] = []
    for note in results:
        if not isinstance(note, dict) or note.get("status") != "settled":
            continue
        summary = note.get("summary")
        if summary is None:
            continue
        evidence.append(
            {
                "tool": str(note.get("tool") or ""),
                "structured_content": sanitize_evidence(summary),
            }
        )
    return evidence


def _goal_scope(params: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any] | None:
    """从 goal.params_json 取 scope 快照（None 字段省略，全空落 None）。"""
    scope = {key: params[key] for key in keys if params.get(key) is not None}
    return scope or None


# warning_code → 报告受限声明（注入模型输入 limitation 键）。
_LIMITATION_NOTES = {"brand_trend_data_unavailable": "趋势数据未成功获取"}


async def _run_goal_analysis(
    db: AsyncSession,
    model: ModelAdapter,
    *,
    purpose: str,
    prompt: PromptTemplate,
    report_type: str,
    scope_keys: tuple[str, ...],
    user_id: str,
    session_id: str,
    task: Any,
    goal: Any,
    thinking_sink: ThinkingSink | None = None,
    warning_code: str | None = None,
) -> AnalysisReport:
    evidence = collect_goal_evidence(getattr(task, "plan_json", None), getattr(goal, "id", None))
    if not evidence:
        raise LookupError("no_evidence_collected")
    params = getattr(goal, "params_json", None)
    if not isinstance(params, dict):
        params = {}
    scope = _goal_scope(params, scope_keys)
    content: dict[str, Any] = {"scope": scope, "evidence": evidence}
    limitation = _LIMITATION_NOTES.get(warning_code) if warning_code else None
    if limitation is not None:
        content["limitation"] = limitation
    result = await model.complete_json(
        StructuredModelRequest(
            purpose=purpose,
            template_name=prompt.name,
            messages=(
                ChatMessage(role="system", content=prompt.system),
                ChatMessage(
                    role="user",
                    content=json.dumps(
                        content,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            ),
            output_model=ReportDocument,
            max_tokens=8192,
            log_context={
                "user_id": user_id,
                "session_id": session_id,
                "task_id": task.id,
                "tags": [purpose],
            },
            thinking_sink=thinking_sink,
        )
    )
    return await AnalysisReportService(db).build_session_report(
        user_id=user_id,
        session_id=session_id,
        document=result.value,
        report_type=report_type,
        scope=scope,
    )


async def run_brand_analysis(
    db: AsyncSession,
    model: ModelAdapter,
    *,
    user_id: str,
    session_id: str,
    task: Any,
    goal: Any,
    thinking_sink: ThinkingSink | None = None,
    warning_code: str | None = None,
) -> AnalysisReport:
    """品牌分析报告 v2：代码组装快照 → 模型叙事 → 快照 + 兼容 Block 一次落库。

    数值事实全部来自 assemble_brand_report 的确定性归一（plan_json settled 证据），
    模型只写叙事层；叙事失败原样上抛（不落成 partial 报告，由 finalize 降级）。
    证据不足抛 ``LookupError("no_evidence_collected")``（由 finalize 映射降级终态）。
    warning_code 合并进 availability 对应章节 reason。thinking_sink 保留在签名中
    （finalize 调用方不变），叙事层暂不支持思考流透传。
    """
    params = goal.params_json if isinstance(getattr(goal, "params_json", None), dict) else {}
    payload = assemble_brand_report(
        getattr(task, "plan_json", None),
        params,
        warning_code=warning_code,
        goal_id=getattr(goal, "id", None),
    )
    narrative = await build_brand_narrative(
        model,
        payload,
        log_context={
            "user_id": user_id,
            "session_id": session_id,
            "task_id": task.id,
            # build_brand_narrative 会自行追加 "brand_report_narrative" 标签。
            "tags": [],
        },
    )
    payload = payload.model_copy(update={"narrative": narrative})
    document = build_brand_compat_document(payload)
    scope = _goal_scope(params, ("brand", "period", "platforms"))
    return await AnalysisReportService(db).build_session_report(
        user_id=user_id,
        session_id=session_id,
        document=document,
        report_type="brand_analysis",
        scope=scope,
        payload=payload.model_dump(mode="json"),
        template_version="brand_report_v2",
    )


# 热帖兼容表的最大行数；日趋势折线受 ChartBlock categories/values 上限约束。
_COMPAT_TOP_POST_ROWS = 10
_COMPAT_TREND_POINTS = 60


def build_brand_compat_document(payload: BrandReportPayload) -> ReportDocument:
    """brand_report_v2 快照 → 兼容 ReportDocument（纯代码，不调模型）。

    供旧 BI/通用报表页等只认 blocks_json 的消费方渲染；缺数据的块整块省略，
    metric_grid（总声量/总互动/覆盖平台/时间窗）恒在，保证 blocks 非空。
    """
    scope = payload.scope
    data = payload.data
    narrative = payload.narrative
    blocks: list[ReportBlock] = []

    overview = data.overview
    period = (
        f"{scope.period_start}~{scope.period_end}"
        if scope.period_start and scope.period_end
        else "未指定"
    )
    blocks.append(
        MetricGridBlock(
            title="综合概览",
            items=[
                MetricItem(
                    label="总声量",
                    value=overview.total_mentions.current
                    if overview.total_mentions.current is not None
                    else "—",
                ),
                MetricItem(
                    label="总互动",
                    value=overview.total_interactions.current
                    if overview.total_interactions.current is not None
                    else "—",
                ),
                MetricItem(
                    label="覆盖平台",
                    value=len(overview.platforms) or len(scope.platforms),
                    unit="个",
                ),
                MetricItem(label="时间窗", value=period),
            ],
        )
    )

    split = overview.sentiment_split
    if any(
        value is not None for value in (split.positive, split.neutral, split.negative)
    ):
        blocks.append(
            ChartBlock(
                type="pie_chart",
                title="情感占比",
                categories=["正面", "中性", "负面"],
                series=[
                    ChartSeries(
                        name="声量",
                        values=[split.positive, split.neutral, split.negative],
                    )
                ],
            )
        )

    points = data.daily_trend.points[-_COMPAT_TREND_POINTS:]
    if points:
        blocks.append(
            ChartBlock(
                type="line_chart",
                title="日趋势",
                categories=[point.date for point in points],
                series=[
                    ChartSeries(name="声量", values=[point.mentions for point in points]),
                    ChartSeries(name="互动数", values=[point.interactions for point in points]),
                ],
            )
        )

    posts = data.top_posts[:_COMPAT_TOP_POST_ROWS]
    if posts:
        blocks.append(
            TableBlock(
                title="热门原帖",
                columns=["平台", "标题", "作者", "互动数"],
                rows=[
                    [post.platform, post.title, post.author, post.interactions] for post in posts
                ],
            )
        )

    if narrative is not None:
        lines = [narrative.conclusion]
        if narrative.recommendations:
            lines.append("")
            lines.extend(f"- {item}" for item in narrative.recommendations)
        blocks.append(MarkdownBlock(text="\n".join(lines)))

    if payload.sources:
        blocks.append(
            SourcesBlock(
                items=[
                    SourceItem(name=source.tool, collected_at=source.collected_at)
                    for source in payload.sources[:20]
                ]
            )
        )

    title = f"{scope.brand} 品牌分析报告" if scope.brand else "品牌分析报告"
    return ReportDocument(
        title=title,
        conclusion=narrative.conclusion if narrative is not None else None,
        blocks=blocks,
    )


async def run_campaign_analysis(
    db: AsyncSession,
    model: ModelAdapter,
    *,
    user_id: str,
    session_id: str,
    task: Any,
    goal: Any,
    thinking_sink: ThinkingSink | None = None,
    warning_code: str | None = None,
) -> AnalysisReport:
    """活动复盘报告：证据聚合 → campaign_analysis_v1 → 落库 report_type=campaign_analysis。"""
    return await _run_goal_analysis(
        db,
        model,
        purpose="campaign_analysis",
        prompt=CAMPAIGN_ANALYSIS_PROMPT,
        report_type="campaign_analysis",
        scope_keys=("brand", "campaign", "period", "platforms"),
        user_id=user_id,
        session_id=session_id,
        task=task,
        goal=goal,
        thinking_sink=thinking_sink,
        warning_code=warning_code,
    )
