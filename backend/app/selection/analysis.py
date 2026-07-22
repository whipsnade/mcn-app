"""手动 KOL 分析：代码聚合圈选名单统计 → 模型撰写 ReportDocument → 会话级落库。

零 MCP 调用、零积分；名单数据全部由 ``KolSelectionService`` 已沉淀的行
在代码侧聚合，模型只负责把统计结果写成报告块。
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.model.contracts import ChatMessage, ModelAdapter, StructuredModelRequest
from app.model.prompts import KOL_ANALYSIS_PROMPT
from app.reporting.analysis_reports import AnalysisReportService
from app.reporting.blocks import ReportDocument
from app.reporting.models import AnalysisReport
from app.selection.models import SessionKolSelection
from app.selection.service import KolSelectionService
from app.tasks.errors import _PLATFORM_LABELS
from app.workspace.models import WorkspaceSession


def _platform_label(platform: str) -> str:
    """平台码 → 中文展示名（复用 tasks.errors 的映射）；未识别的码原样保留。"""
    return _PLATFORM_LABELS.get(platform, platform)


_RATING_KEYS = ("重点推荐", "推荐", "可考虑", "观察")
_FOLLOWERS_BUCKET_KEYS = ("<10万", "10-50万", "50-100万", "100-500万", ">500万")
# 评分理由字段：export_fields 规范键优先，兼容中文原始键。
_SCORE_REASON_KEYS = ("score_reason", "评分理由")


def _followers_bucket(followers: int) -> str:
    if followers < 100_000:
        return "<10万"
    if followers < 500_000:
        return "10-50万"
    if followers < 1_000_000:
        return "50-100万"
    if followers < 5_000_000:
        return "100-500万"
    return ">500万"


def _score_reason(row: SessionKolSelection) -> str:
    export_fields = (row.fields_json or {}).get("export_fields") or {}
    for key in _SCORE_REASON_KEYS:
        value = export_fields.get(key)
        if value:
            return str(value)
    return ""


def build_kol_analysis_summary(
    rows: list[SessionKolSelection],
    *,
    brand: str,
    category: str | None,
    target_audience: str,
) -> dict[str, Any]:
    """把圈选名单行聚合为模型输入统计（纯函数，rows 已按总分倒序）。"""
    platform_counts: dict[str, int] = {}
    rating_counts = {key: 0 for key in _RATING_KEYS}
    followers_buckets = {key: 0 for key in _FOLLOWERS_BUCKET_KEYS}
    city_counts: dict[str, int] = {}
    scores: list[float] = []
    for row in rows:
        label = _platform_label(row.platform)
        platform_counts[label] = platform_counts.get(label, 0) + 1
        score_json = row.score_json or {}
        rating = score_json.get("rating")
        if rating in rating_counts:
            rating_counts[rating] += 1
        if row.followers is not None:
            followers_buckets[_followers_bucket(row.followers)] += 1
        if row.city:
            city_counts[row.city] = city_counts.get(row.city, 0) + 1
        total = score_json.get("total")
        if isinstance(total, int | float):
            scores.append(float(total))
    city_top10 = [
        {"city": city, "count": count}
        for city, count in sorted(
            city_counts.items(), key=lambda item: (-item[1], item[0])
        )[:10]
    ]
    top10 = [
        {
            "nickname": row.nickname,
            "platform": _platform_label(row.platform),
            "followers": row.followers,
            "total_score": float((row.score_json or {}).get("total") or 0.0),
            "rating": (row.score_json or {}).get("rating") or "",
            "score_reason": _score_reason(row),
        }
        for row in rows[:10]
    ]
    return {
        "total": len(rows),
        "platform_counts": platform_counts,
        "rating_counts": rating_counts,
        "followers_buckets": followers_buckets,
        "avg_score": round(sum(scores) / len(scores), 1) if scores else 0,
        "city_top10": city_top10,
        "top10": top10,
        "brand": brand,
        "category": category,
        "target_audience": target_audience,
    }


async def run_kol_analysis(
    db: AsyncSession,
    model: ModelAdapter,
    *,
    user_id: str,
    session_id: str,
) -> AnalysisReport:
    """聚合名单 → 模型撰写报告 → 会话级落库，同步返回报告。

    错误契约：
    - ``LookupError("session_not_found")``：会话不存在/不属于该用户/已软删；
    - ``LookupError("no_kol_selection")``：名单为空；
    - ``LookupError("report_version_conflict")``：落库版本冲突（来自报告服务）。
    """
    rows = await KolSelectionService(db).get_all_for_export(
        user_id=user_id, session_id=session_id
    )
    if not rows:
        raise LookupError("no_kol_selection")
    session = await db.get(WorkspaceSession, session_id)
    summary = build_kol_analysis_summary(
        rows,
        brand=session.brand if session else "",
        category=session.category if session else None,
        target_audience=session.target_audience if session else "",
    )
    result = await model.complete_json(
        StructuredModelRequest(
            purpose="kol_analysis",
            template_name=KOL_ANALYSIS_PROMPT.name,
            messages=(
                ChatMessage(role="system", content=KOL_ANALYSIS_PROMPT.system),
                ChatMessage(
                    role="user",
                    content=json.dumps(summary, ensure_ascii=False),
                ),
            ),
            output_model=ReportDocument,
            max_tokens=8192,
            log_context={
                "user_id": user_id,
                "session_id": session_id,
                "tags": ["kol_analysis"],
            },
        )
    )
    return await AnalysisReportService(db).build_session_report(
        user_id=user_id,
        session_id=session_id,
        document=result.value,
    )


__all__ = ["build_kol_analysis_summary", "run_kol_analysis"]
