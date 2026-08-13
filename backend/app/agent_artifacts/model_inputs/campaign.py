"""campaign_report_v2/v3 模型输入 DTO 与服务器端组装（提交 1）。

campaign_report_v2 与 campaign_report_v3 共享同一 DTO；``schema_version``
由 :func:`assemble_campaign_payload` 参数化（v3 是 Direct Artifact Skill
契约，v2 保持当前/兼容契约）。canonical 生成与 brand 完全一致。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.agent_artifacts.canonical import (
    model_direct_lineage_context,
    publish_canonical,
)
from app.agent_artifacts.model_inputs.brand import (
    BrandMethodologyInput,
    _derive_data_status,
    _partial_leaf_paths,
)
from app.agent_artifacts.payloads.campaign import (
    CampaignData,
    CampaignNarrative,
    CampaignReportV2,
    CampaignScope,
)
from app.agent_artifacts.payloads.common import Limitation, SectionAvailability

#: campaign 与 brand 的 methodology 输入结构相同（复用，不复制定义）。
CampaignMethodologyInput = BrandMethodologyInput


class CampaignReportV3Input(BaseModel):
    """campaign_report_v2/v3 模型输入契约（不含任何服务器字段）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: CampaignScope
    data: CampaignData
    narrative: CampaignNarrative
    availability: dict[str, SectionAvailability]
    limitations: tuple[Limitation, ...] = Field(default_factory=tuple)
    methodology_input: CampaignMethodologyInput

    @classmethod
    def concise_example(cls) -> dict[str, Any]:
        """合法最小模型输入（业务值为简单占位，禁止含服务器字段）。"""
        return {
            "scope": {
                "brand": "示例品牌",
                "campaign": "示例活动",
                "period": {
                    "start": "2026-01-01",
                    "end": "2026-01-31",
                    "timezone": "Asia/Shanghai",
                },
                "platforms": ["xiaohongshu"],
                "keywords": [],
            },
            "data": {
                "overview": {
                    "total_volume": 100,
                    "total_engagement": 200,
                    "total_posts": 10,
                    "total_creators": 3,
                    "sentiment_score": 0.5,
                },
                "platform_contributions": [],
                "timeline": [],
                "kol_contributions": [],
                "content_types": [],
                "sentiment": {
                    "summary": {
                        "positive": {"count": 10, "share": 0.5},
                        "neutral": {"count": 4, "share": 0.2},
                        "negative": {"count": 6, "share": 0.3},
                    },
                    "by_platform": [],
                },
                "top_posts": [],
            },
            "narrative": {
                "executive_summary": "示例摘要",
                "phase_review": [],
                "findings": [],
                "recommendations": [],
            },
            "availability": {
                section: {"status": "complete", "reason_codes": []}
                for section in (
                    "overview",
                    "platform_contributions",
                    "timeline",
                    "sentiment",
                    "top_posts",
                )
            },
            "limitations": [],
            "methodology_input": {
                "data_as_of": "2026-01-15T12:00:00",
                "source_names": ["DataTap"],
                "notes": [],
            },
        }


def assemble_campaign_payload(
    model_input: CampaignReportV3Input, *, schema_version: str
) -> dict[str, Any]:
    """模型输入 → 完整 campaign_report_v2/v3 发布 payload（canonical 精确覆盖）。"""
    if schema_version not in {"campaign_report_v2", "campaign_report_v3"}:
        raise ValueError(f"unsupported campaign schema_version {schema_version!r}")
    data_dump = model_input.data.model_dump(mode="json")
    partial_paths = _partial_leaf_paths(model_input.availability, data_dump)
    with model_direct_lineage_context():
        fields, lineage = publish_canonical(
            data=data_dump,
            refs=[],
            partial_paths=partial_paths,
            module="campaign",
        )
    payload: dict[str, Any] = {
        "schema_version": schema_version,
        "module": "campaign",
        "data_status": _derive_data_status(
            model_input.availability, CampaignReportV2.REQUIRED_SECTIONS
        ),
        "availability": {
            section: entry.model_dump(mode="json")
            for section, entry in model_input.availability.items()
        },
        "limitations": [item.model_dump(mode="json") for item in model_input.limitations],
        "methodology": model_input.methodology_input.model_dump(mode="json"),
        "scope": model_input.scope.model_dump(mode="json"),
        "data": data_dump,
        "narrative": model_input.narrative.model_dump(mode="json"),
        "canonical_data": [field.model_dump(mode="json") for field in fields],
        "field_lineage": {path: list(targets) for path, targets in lineage.items()},
    }
    return payload


__all__ = [
    "CampaignMethodologyInput",
    "CampaignReportV3Input",
    "assemble_campaign_payload",
]
