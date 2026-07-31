"""brand_report_v2 叙事层：结构化数据唯一输入的模型撰写（Task 5）。

模型输入只有 payload.scope/query_spec/data/availability（JSON）——scope 与
query_spec 提供主语、时间窗与数据截至日（无数值指标），data 仍是唯一数值
事实来源；原始证据与 sources（内部 step_id）不进 prompt。输出经
BrandReportNarrative 校验，异常（ModelPlanInvalidError）直接上抛，由调用方
走失败 Artifact 路径。BrandReportNarrative 模型定义在 brand_payload.py
（避免双向 import 循环），此处 re-export 便于调用方单点导入。
"""

from __future__ import annotations

import json
from typing import Any

from app.model.contracts import ChatMessage, ModelAdapter, StructuredModelRequest
from app.model.prompts import BRAND_REPORT_NARRATIVE_PROMPT
from app.reporting.brand_payload import BrandReportNarrative, BrandReportPayload

__all__ = ["BrandReportNarrative", "build_brand_narrative"]


async def build_brand_narrative(
    model: ModelAdapter,
    payload: BrandReportPayload,
    *,
    log_context: dict[str, Any],
) -> BrandReportNarrative:
    """用 brand_report_narrative_v1 撰写叙事。

    purpose="brand_report_narrative"，log_context.tags 追加同名标签，
    经 complete_json 统一出口落 model_prompt_logs。
    """
    content = {
        "scope": payload.scope.model_dump(mode="json"),
        "query_spec": payload.query_spec.model_dump(mode="json"),
        "data": payload.data.model_dump(mode="json"),
        "availability": {
            chapter: availability.model_dump(mode="json")
            for chapter, availability in payload.availability.items()
        },
    }
    tags = [*(log_context.get("tags") or []), "brand_report_narrative"]
    result = await model.complete_json(
        StructuredModelRequest(
            purpose="brand_report_narrative",
            template_name=BRAND_REPORT_NARRATIVE_PROMPT.name,
            messages=(
                ChatMessage(role="system", content=BRAND_REPORT_NARRATIVE_PROMPT.system),
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
            output_model=BrandReportNarrative,
            max_tokens=4096,
            log_context={**log_context, "tags": tags},
        )
    )
    return result.value
