"""brand_report_v3 模型输入 DTO 与服务器端组装（提交 1）。

模型只提交业务字段（scope/data/narrative/availability/limitations/
methodology_input）；服务器推导 ``data_status``、组装 ``methodology``，
并用 :func:`publish_canonical` 确定性生成覆盖全部 ``data`` 叶子的
``canonical_data`` / ``field_lineage``——模型不再手写任何服务器字段。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.agent_artifacts.canonical import (
    model_direct_lineage_context,
    publish_canonical,
    walk_data_leaves,
)
from app.agent_artifacts.payloads.brand import (
    BrandData,
    BrandNarrative,
    BrandReportV3,
    BrandScope,
)
from app.agent_artifacts.payloads.common import Limitation, SectionAvailability


class BrandMethodologyInput(BaseModel):
    """模型的来源/时间说明；服务器组装为发布 payload 的 ``methodology``。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    data_as_of: datetime
    source_names: tuple[str, ...] = Field(default_factory=tuple)
    notes: tuple[str, ...] = Field(default_factory=tuple)


class BrandReportV3Input(BaseModel):
    """brand_report_v3 模型输入契约（不含任何服务器字段）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: BrandScope
    data: BrandData
    narrative: BrandNarrative
    availability: dict[str, SectionAvailability]
    limitations: tuple[Limitation, ...] = Field(default_factory=tuple)
    methodology_input: BrandMethodologyInput

    @classmethod
    def concise_example(cls) -> dict[str, Any]:
        """合法最小模型输入（业务值为简单占位，禁止含服务器字段）。"""
        return {
            "scope": {
                "brand": "示例品牌",
                "period": {
                    "start": "2026-01-01",
                    "end": "2026-01-31",
                    "timezone": "Asia/Shanghai",
                },
                "platforms": ["xiaohongshu"],
                "keywords": ["示例"],
                "comparison_mode": "none",
            },
            "data": {
                "overview": {
                    "total_volume": 100,
                    "total_engagement": 200,
                    "total_posts": 10,
                    "sentiment_score": 0.5,
                    "platforms": [
                        {
                            "platform": "xiaohongshu",
                            "volume": 100,
                            "engagement": 200,
                            "posts": 10,
                            "share_of_voice": 1.0,
                            "sentiment_score": 0.5,
                        }
                    ],
                },
                "comparisons": {
                    "mom": {"status": "not_requested", "metrics": []},
                    "yoy": {"status": "not_requested", "metrics": []},
                },
                "sentiment": {
                    "summary": {
                        "positive": {"count": 10, "share": 0.5},
                        "neutral": {"count": 4, "share": 0.2},
                        "negative": {"count": 6, "share": 0.3},
                    },
                    "by_platform": [],
                },
                "daily_trend": [],
                "content_types": [],
                "creator_tiers": [],
                "organic_vs_paid": [],
                "regions": [],
                "topics": [],
                "top_posts": [],
            },
            "narrative": {
                "executive_summary": "示例摘要",
                "findings": [],
                "recommendations": [],
            },
            "availability": {
                section: {"status": "complete", "reason_codes": []}
                for section in ("overview", "sentiment", "daily_trend", "topics", "top_posts")
            },
            "limitations": [],
            "methodology_input": {
                "data_as_of": "2026-01-15T12:00:00",
                "source_names": ["DataTap"],
                "notes": [],
            },
        }


def _derive_data_status(
    availability: dict[str, SectionAvailability], required: frozenset[str]
) -> str:
    """§2.5 反向聚合：全部必需章节存在且 complete → complete，否则 restricted。"""
    if all(
        section in availability and availability[section].status == "complete"
        for section in required
    ):
        return "complete"
    return "restricted"


def _partial_leaf_paths(
    availability: dict[str, SectionAvailability], data_dump: dict[str, Any]
) -> frozenset[str]:
    """availability 中非 complete 章节在 ``data`` 下的全部叶子路径。"""
    leaves = walk_data_leaves(data_dump)
    return frozenset(
        path
        for section, entry in availability.items()
        if entry.status != "complete"
        for path, _value in leaves
        if path.startswith(f"/data/{section}")
    )


def assemble_brand_payload(model_input: BrandReportV3Input) -> dict[str, Any]:
    """模型输入 → 完整 brand_report_v3 发布 payload（canonical 精确覆盖）。"""
    data_dump = model_input.data.model_dump(mode="json")
    partial_paths = _partial_leaf_paths(model_input.availability, data_dump)
    with model_direct_lineage_context():
        fields, lineage = publish_canonical(
            data=data_dump,
            refs=[],
            partial_paths=partial_paths,
            module="brand",
        )
    payload: dict[str, Any] = {
        "schema_version": "brand_report_v3",
        "module": "brand",
        "data_status": _derive_data_status(model_input.availability, BrandReportV3.REQUIRED_SECTIONS),
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
    "BrandMethodologyInput",
    "BrandReportV3Input",
    "assemble_brand_payload",
]
