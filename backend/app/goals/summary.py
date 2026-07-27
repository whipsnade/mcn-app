"""Goal 摘要生成器：上游 goal 收尾后的精简摘要，供下游 goal 注入（阶段四编排）。

模型路径失败或证据为空时回退纯代码摘要（按工具名分组计数），绝不抛异常阻塞编排。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.model.contracts import ChatMessage, ModelAdapter, StructuredModelRequest
from app.model.prompts import GOAL_SUMMARY_PROMPT


logger = logging.getLogger(__name__)


class GoalResultSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=1000)
    highlights: dict[str, Any] = Field(default_factory=dict)


def _fallback_summary(evidence: list[dict[str, Any]]) -> str:
    """纯代码摘要：按工具名分组统计条数。"""
    if not evidence:
        return "本 goal 未采集到有效工具证据。"
    counts: dict[str, int] = {}
    for item in evidence:
        tool = str(item.get("tool") or "unknown")
        counts[tool] = counts.get(tool, 0) + 1
    parts = "、".join(f"{tool} {count} 条" for tool, count in counts.items())
    return f"共采集 {len(evidence)} 条工具证据：{parts}。"


async def build_goal_result_summary(
    model: ModelAdapter | None,
    *,
    goal_type: str,
    scope: dict[str, Any] | None,
    evidence: list[dict[str, Any]],
    artifact: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """生成 goal 结果摘要：{summary, highlights, artifact(原样透传)}。

    evidence 为该 goal 的 settled 证据列表（{tool, structured_content}，
    由调用方从轨迹切片提取）。模型异常/无模型/证据为空一律回退代码摘要。
    """
    if model is not None and evidence:
        try:
            result = await model.complete_json(
                StructuredModelRequest(
                    purpose="goal_summary",
                    template_name=GOAL_SUMMARY_PROMPT.name,
                    messages=(
                        ChatMessage(role="system", content=GOAL_SUMMARY_PROMPT.system),
                        ChatMessage(
                            role="user",
                            content=json.dumps(
                                {
                                    "goal_type": goal_type,
                                    "scope": scope,
                                    "evidence": evidence,
                                },
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                        ),
                    ),
                    output_model=GoalResultSummary,
                    # 推理模型 <think> 占用输出预算，1024 几乎必然截断 JSON 走代码摘要兜底。
                    max_tokens=2_048,
                    log_context={"tags": ["goal_summary"]},
                )
            )
            return {
                "summary": result.value.summary,
                "highlights": result.value.highlights,
                "artifact": artifact,
            }
        except Exception:
            logger.warning(
                "goal_result_summary_fallback goal_type=%s", goal_type, exc_info=True
            )
    return {
        "summary": _fallback_summary(evidence),
        "highlights": {},
        "artifact": artifact,
    }
