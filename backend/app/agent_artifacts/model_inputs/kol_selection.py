"""kol_selection_v3 模型输入 DTO 与服务器端组装（提交 1）。

kol_selection_v3 不是 :class:`CanonicalPayloadMixin`（名单 payload 不携带
canonical 契约），组装输出不包含 canonical_data/field_lineage——其余
服务器字段（schema_version/module/data_status/methodology）照常推导。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.agent_artifacts.model_inputs.brand import (
    BrandMethodologyInput,
    _derive_data_status,
)
from app.agent_artifacts.payloads.common import Limitation, SectionAvailability
from app.agent_artifacts.payloads.kol_selection import (
    KolSelectionData,
    KolSelectionNarrative,
    KolSelectionScopeV3,
    KolSelectionV3,
    WEIGHTS as KOL_V2_WEIGHTS,
)

KolSelectionMethodologyInput = BrandMethodologyInput


class KolSelectionV3Input(BaseModel):
    """kol_selection_v3 模型输入契约（不含任何服务器字段）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: KolSelectionScopeV3
    data: KolSelectionData
    narrative: KolSelectionNarrative
    availability: dict[str, SectionAvailability]
    limitations: tuple[Limitation, ...] = Field(default_factory=tuple)
    methodology_input: KolSelectionMethodologyInput

    @classmethod
    def concise_example(cls) -> dict[str, Any]:
        """合法最小模型输入（业务值为简单占位，禁止含服务器字段）。"""
        return {
            "scope": {
                "platforms": ["xiaohongshu"],
                "audience": {"regions": ["上海"], "age_ranges": [], "interests": []},
                "filters": {},
            },
            "data": {
                "scoring": {
                    "version": "kol_score_v2",
                    "method": "weighted_sum",
                    "weights": dict(KOL_V2_WEIGHTS),
                    "missing_value_policy": "missing_as_zero",
                },
                "items": [],
                "summary": {
                    "candidate_count": 0,
                    "selected_count": 0,
                    "platform_distribution": [],
                    "rating_distribution": [],
                },
            },
            "narrative": {
                "selection_summary": "示例摘要",
                "fit_findings": [],
                "risk_notes": [],
                "usage_advice": [],
            },
            "availability": {
                section: {"status": "complete", "reason_codes": []}
                for section in ("scoring", "items", "summary")
            },
            "limitations": [],
            "methodology_input": {
                "data_as_of": "2026-01-15T12:00:00",
                "source_names": ["DataTap"],
                "notes": [],
            },
        }


def assemble_kol_selection_payload(model_input: KolSelectionV3Input) -> dict[str, Any]:
    """模型输入 → 完整 kol_selection_v3 发布 payload（无 canonical）。"""
    payload: dict[str, Any] = {
        "schema_version": "kol_selection_v3",
        "module": "kol",
        "data_status": _derive_data_status(
            model_input.availability, KolSelectionV3.REQUIRED_SECTIONS
        ),
        "availability": {
            section: entry.model_dump(mode="json")
            for section, entry in model_input.availability.items()
        },
        "limitations": [item.model_dump(mode="json") for item in model_input.limitations],
        "methodology": model_input.methodology_input.model_dump(mode="json"),
        "scope": model_input.scope.model_dump(mode="json"),
        "data": model_input.data.model_dump(mode="json"),
        "narrative": model_input.narrative.model_dump(mode="json"),
    }
    return payload


__all__ = [
    "KolSelectionMethodologyInput",
    "KolSelectionV3Input",
    "assemble_kol_selection_payload",
]
