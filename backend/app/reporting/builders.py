"""品牌/活动报告构建器：轨迹证据聚合 → 模型撰写 ReportDocument → 会话级落库。

参照 run_kol_analysis 的事务模式：证据先从 task.plan_json 读出（纯内存），
模型调用不持有数据库事务，落库走 build_session_report（按 report_type 独立编号）。
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
from app.model.prompts import (
    BRAND_ANALYSIS_PROMPT,
    CAMPAIGN_ANALYSIS_PROMPT,
    PromptTemplate,
)
from app.reporting.analysis_reports import AnalysisReportService, sanitize_evidence
from app.reporting.blocks import ReportDocument
from app.reporting.models import AnalysisReport


def collect_goal_evidence(task_plan_json: dict[str, Any] | None) -> list[dict[str, Any]]:
    """从 plan_json（agent_trajectory_v1）提取 settled 证据并脱敏截断。

    返回 [{"tool": 内部工具名, "structured_content": 脱敏后的证据}]；
    无轨迹或无 settled 证据返回 []。
    """
    if not isinstance(task_plan_json, dict):
        return []
    results = task_plan_json.get("results")
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
) -> AnalysisReport:
    evidence = collect_goal_evidence(getattr(task, "plan_json", None))
    if not evidence:
        raise LookupError("no_evidence_collected")
    params = getattr(goal, "params_json", None)
    if not isinstance(params, dict):
        params = {}
    scope = _goal_scope(params, scope_keys)
    result = await model.complete_json(
        StructuredModelRequest(
            purpose=purpose,
            template_name=prompt.name,
            messages=(
                ChatMessage(role="system", content=prompt.system),
                ChatMessage(
                    role="user",
                    content=json.dumps(
                        {"scope": scope, "evidence": evidence},
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
) -> AnalysisReport:
    """品牌分析报告：证据聚合 → brand_analysis_v1 → 落库 report_type=brand_analysis。

    证据为空抛 ``LookupError("no_evidence_collected")``（由 finalize 映射降级终态）。
    """
    return await _run_goal_analysis(
        db,
        model,
        purpose="brand_analysis",
        prompt=BRAND_ANALYSIS_PROMPT,
        report_type="brand_analysis",
        scope_keys=("brand", "period", "platforms"),
        user_id=user_id,
        session_id=session_id,
        task=task,
        goal=goal,
        thinking_sink=thinking_sink,
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
    )
