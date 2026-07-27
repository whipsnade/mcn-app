from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.goals.context import recent_task_outcomes
from app.model.contracts import ChatMessage, ModelAdapter, StructuredModelRequest
from app.model.prompts import CONTEXT_QA_PROMPT
from app.orchestration.context import compress_messages
from app.reporting.analysis_reports import AnalysisReportService
from app.selection.models import KolSelectionItem, KolSelectionSet
from app.selection.scoring import rating
from app.workspace.models import Message


logger = logging.getLogger(__name__)

USAGE_GUIDE_TEXT = """KOL Insight AI 使用方法：

1. 新建会话后，先通过问答确认分析需求（品牌、品类、平台、目标），信息齐全后自动开始分析。
2. 直接输入分析需求即可，例如：
   -「分析一下 Manner 咖啡近三个月的声量和情感趋势」（品牌分析）
   -「评估 Manner 夏日冷萃活动的传播效果」（活动分析）
   -「帮我圈选适合咖啡品牌的杭州本地美食达人，预算 5 万」（达人圈选）
3. 分析完成后，在右侧 BI 面板查看品牌分析、活动分析和达人名单；达人名单可导出 Excel。
4. 会话列表下方的快捷按钮：达人推荐（按预算）、达人/活动评估、小红书/抖音前十爆贴。
5. 每次数据查询消耗 10 积分，余额不足时分析会暂停；可在钱包查看积分流水。"""

OUT_OF_SCOPE_TEXT = (
    "抱歉，我是营销分析助手，只支持 KOL 达人、品牌、活动相关的营销分析，"
    "以及本会话历史内容的问答，其他话题无法提供帮助。"
)

CONTEXT_QA_FALLBACK_TEXT = "暂时无法回答，请稍后重试。"

_EVIDENCE_MAX_CHARS = 12_000
_RECENT_MESSAGES_MAX_CHARS = 6_000
_REPORT_MAX_CHARS = 4_000
_SELECTION_TOP_N = 20


class ContextQaAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str = Field(min_length=1, max_length=4_000)


async def _selection_projection(db, session_id: str) -> list[dict[str, Any]]:
    latest_set = await db.scalar(
        select(KolSelectionSet)
        .where(KolSelectionSet.session_id == session_id)
        .order_by(KolSelectionSet.version.desc())
        .limit(1)
    )
    if latest_set is None:
        return []
    # 圈选名单量级 ≤50，全取后在 Python 内按总分倒序取 top 20；
    # score_json 无 total（或非数值）的达人排最后，保持稳定入库序。
    items = list(
        (
            await db.scalars(
                select(KolSelectionItem)
                .where(KolSelectionItem.selection_set_id == latest_set.id)
                .order_by(KolSelectionItem.created_at)
            )
        ).all()
    )

    def _total(item: KolSelectionItem) -> float | None:
        total = (item.score_json or {}).get("total")
        return float(total) if isinstance(total, (int, float)) else None

    items.sort(
        key=lambda item: (_total(item) is None, -(_total(item) or 0.0)),
    )
    projection: list[dict[str, Any]] = []
    for item in items[:_SELECTION_TOP_N]:
        total = _total(item)
        label = rating(total)[0] if total is not None else None
        projection.append(
            {
                "platform": item.platform,
                "nickname": item.nickname,
                "followers": item.followers,
                "city": item.city,
                "total_score": total,
                "rating": label,
            }
        )
    return projection


async def _report_projections(db, session_id: str) -> list[dict[str, Any]]:
    service = AnalysisReportService(db)
    projections: list[dict[str, Any]] = []
    for report_type in ("kol_analysis", "brand_analysis", "campaign_analysis"):
        report = await service.latest_session_report(session_id, report_type=report_type)
        if report is None:
            continue
        projections.append(
            {
                "report_type": report_type,
                "title": report.title,
                "version": report.version,
                "content": json.dumps(
                    report.blocks_json, ensure_ascii=False, separators=(",", ":")
                )[:_REPORT_MAX_CHARS],
            }
        )
    return projections


def _fit_budget(evidence: dict[str, Any]) -> dict[str, Any]:
    """证据包总量上限：超出时按 报告正文 → 报告 → 名单 顺序裁剪。"""
    if len(json.dumps(evidence, ensure_ascii=False)) <= _EVIDENCE_MAX_CHARS:
        return evidence
    for report in evidence.get("reports", []):
        report["content"] = report["content"][:1500]
    if len(json.dumps(evidence, ensure_ascii=False)) <= _EVIDENCE_MAX_CHARS:
        return evidence
    evidence["reports"] = []
    if len(json.dumps(evidence, ensure_ascii=False)) <= _EVIDENCE_MAX_CHARS:
        return evidence
    evidence["selection"] = evidence["selection"][:5]
    return evidence


async def _recent_messages_projection(
    db, *, user_id: str, session_id: str
) -> list[dict[str, Any]]:
    history = list(
        (
            await db.scalars(
                select(Message)
                .where(
                    Message.session_id == session_id,
                    Message.user_id == user_id,
                )
                .order_by(Message.sequence.desc())
                .limit(20)
            )
        ).all()
    )
    history.reverse()
    return [
        message.model_dump(mode="json")
        for message in compress_messages(history, max_chars=_RECENT_MESSAGES_MAX_CHARS)
    ]


async def build_context_qa_evidence(
    db, *, user_id: str, session_id: str
) -> dict[str, Any]:
    """组装 context_qa 证据包；调用方需已完成会话归属校验。"""
    evidence: dict[str, Any] = {
        "recent_messages": await _recent_messages_projection(
            db, user_id=user_id, session_id=session_id
        ),
        "recent_task_outcomes": await recent_task_outcomes(db, user_id, session_id),
        "selection": await _selection_projection(db, session_id),
        "reports": await _report_projections(db, session_id),
    }
    return _fit_budget(evidence)


async def answer_context_qa(
    db,
    model: ModelAdapter,
    *,
    user_id: str,
    session_id: str,
    question: str,
) -> str:
    """一次零积分模型调用回答上下文提问；任何失败降级固定文案。"""
    try:
        evidence = await build_context_qa_evidence(
            db, user_id=user_id, session_id=session_id
        )
    except Exception:
        # spec 降级语义：证据包组装失败时降级为仅最近消息；再失败给空包。
        logger.warning("context_qa_evidence_failed session_id=%s", session_id, exc_info=True)
        try:
            evidence = {
                "recent_messages": await _recent_messages_projection(
                    db, user_id=user_id, session_id=session_id
                )
            }
        except Exception:
            logger.warning(
                "context_qa_recent_messages_failed session_id=%s",
                session_id,
                exc_info=True,
            )
            evidence = {}
    try:
        result = await model.complete_json(
            StructuredModelRequest(
                purpose="context_qa",
                template_name=CONTEXT_QA_PROMPT.name,
                messages=(
                    ChatMessage(role="system", content=CONTEXT_QA_PROMPT.system),
                    ChatMessage(
                        role="user",
                        content=json.dumps(
                            {"question": question, "evidence": evidence},
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    ),
                ),
                output_model=ContextQaAnswer,
                max_tokens=2048,
                log_context={"user_id": user_id, "session_id": session_id},
            )
        )
        return result.value.answer.strip() or CONTEXT_QA_FALLBACK_TEXT
    except Exception:
        logger.warning("context_qa_model_failed session_id=%s", session_id, exc_info=True)
        return CONTEXT_QA_FALLBACK_TEXT
