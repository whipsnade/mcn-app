"""`analysis_report_v1` 的模型输入 DTO 与服务器组装器。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent_artifacts.payloads.analysis_report import (
    AnalysisReportBlock,
    AnalysisReportFulfillment,
    AnalysisReportWorkbookLayout,
    _aggregate_fulfillment_status,
    _has_limitation,
    _reject_unsafe_text,
)
from app.agent_artifacts.payloads.common import Limitation, SectionAvailability


class AnalysisReportMethodologyInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    data_as_of: datetime
    source_names: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


class AnalysisReportV1Input(BaseModel):
    """模型只提交业务字段；schema/module/data_status 等由服务器补齐。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(min_length=1, max_length=512)
    subject_type: Literal["brand", "campaign", "kol", "mixed"]
    scope: dict[str, Any]
    blocks: tuple[AnalysisReportBlock, ...] = ()
    fulfillment: tuple[AnalysisReportFulfillment, ...] = ()
    availability: dict[str, SectionAvailability]
    limitations: tuple[Limitation, ...] = ()
    methodology_input: AnalysisReportMethodologyInput
    workbook: AnalysisReportWorkbookLayout | None = None

    @model_validator(mode="after")
    def validate_business_input(self) -> AnalysisReportV1Input:
        ids = [block.id for block in self.blocks]
        if len(set(ids)) != len(ids):
            raise ValueError("analysis report block ids must be unique")
        if "blocks" not in self.availability:
            raise ValueError("availability must include blocks")
        _reject_unsafe_text(self.scope, "scope")
        _reject_unsafe_text(self.blocks, "blocks")
        _reject_unsafe_text(self.fulfillment, "fulfillment")
        _reject_unsafe_text(self.title, "title")
        _reject_unsafe_text(self.limitations, "limitations")
        _reject_unsafe_text(self.methodology_input, "methodology_input")
        _reject_unsafe_text(self.workbook, "workbook")
        return self

    @classmethod
    def concise_example(cls) -> dict[str, Any]:
        return {
            "title": "跨平台营销分析",
            "subject_type": "mixed",
            "scope": {"brand": "示例品牌", "platforms": ["xiaohongshu"]},
            "blocks": [{
                "block_type": "narrative",
                "id": "summary",
                "title": "摘要",
                "content": "数据已按真实返回结果整理。",
                "supporting_paths": [],
            }],
            "fulfillment": [{
                "key": "requested_items",
                "requested_min": 0,
                "actual_count": 0,
                "status": "complete",
                "reason": "本请求未要求固定数量",
            }],
            "availability": {"blocks": {"status": "complete", "reason_codes": []}},
            "limitations": [],
            "methodology_input": {
                "data_as_of": "2026-01-15T12:00:00",
                "source_names": ["DataTap"],
                "notes": [],
            },
        }


def assemble_analysis_report_payload(model_input: AnalysisReportV1Input) -> dict[str, Any]:
    """将模型业务输入组装为完整、可发布的 `analysis_report_v1` payload。"""
    availability = dict(model_input.availability)
    fulfillment_status = _aggregate_fulfillment_status(model_input.fulfillment)
    availability["fulfillment"] = SectionAvailability(
        status=fulfillment_status,
        reason_codes=(f"fulfillment_{fulfillment_status}",),
    )
    limitations = list(model_input.limitations)
    for section, entry in availability.items():
        if entry.status != "complete" and not _has_limitation(tuple(limitations), section):
            limitations.append(
                Limitation(
                    code=f"{section}_{entry.status}",
                    message=f"{section} 数据状态为 {entry.status}，保留真实返回结果。",
                    affected_paths=(section,),
                )
            )
    data_status = (
        "complete"
        if all(availability[section].status == "complete" for section in ("blocks", "fulfillment"))
        else "restricted"
    )
    return {
        "schema_version": "analysis_report_v1",
        "module": "report",
        "data_status": data_status,
        "availability": {
            section: entry.model_dump(mode="json")
            for section, entry in availability.items()
        },
        "limitations": [item.model_dump(mode="json") for item in limitations],
        "methodology": model_input.methodology_input.model_dump(mode="json"),
        "title": model_input.title,
        "subject_type": model_input.subject_type,
        "scope": model_input.scope,
        "blocks": [block.model_dump(mode="json") for block in model_input.blocks],
        "fulfillment": [item.model_dump(mode="json") for item in model_input.fulfillment],
        "workbook": model_input.workbook.model_dump(mode="json") if model_input.workbook else None,
    }


__all__ = [
    "AnalysisReportMethodologyInput",
    "AnalysisReportV1Input",
    "assemble_analysis_report_payload",
]
