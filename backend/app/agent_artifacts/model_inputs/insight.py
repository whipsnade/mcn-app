"""insight_board_v1 模型输入 DTO 与服务器端组装（提交 1）。

insight_board_v1 不是 :class:`CanonicalPayloadMixin`，组装输出不包含
canonical_data/field_lineage。payload 的 ``data`` 字段直接来自模型输入的
``blocks``（看板板块序列），``module`` 复用父 Artifact 语义
（brand/campaign/kol，与既有 Builder 的 ``_PAYLOAD_MODULE_BY_PARENT``
默认一致），模型可在输入中显式声明钻取对象侧。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agent_artifacts.model_inputs.brand import (
    BrandMethodologyInput,
    _derive_data_status,
)
from app.agent_artifacts.payloads.common import Limitation, SectionAvailability
from app.agent_artifacts.payloads.insight import (
    InsightBlock,
    InsightBoardV1,
    InsightNarrative,
    InsightScope,
)

InsightMethodologyInput = BrandMethodologyInput


class InsightBoardV1Input(BaseModel):
    """insight_board_v1 模型输入契约（不含 schema/data_status/canonical 等服务器字段）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # payload module 是 ArtifactPayloadBase 的 Literal["brand","campaign","kol"]；
    # 模型输入显式声明钻取对象侧，缺省 brand（与既有 insight fixture 一致）。
    module: Literal["brand", "campaign", "kol"] = "brand"
    title: str
    scope: InsightScope
    parent_artifact_id: str
    parent_artifact_version_id: str | None = None
    narrative: InsightNarrative
    # 对应发布 payload 的 data 字段（看板板块序列）。
    blocks: tuple[InsightBlock, ...] = Field(default_factory=tuple, max_length=50)
    availability: dict[str, SectionAvailability]
    limitations: tuple[Limitation, ...] = Field(default_factory=tuple)
    methodology_input: InsightMethodologyInput

    @classmethod
    def concise_example(cls) -> dict[str, Any]:
        """合法最小模型输入（markdown 板块无数字叶子，禁止含服务器字段）。"""
        return {
            "title": "钻取结论",
            "scope": {"summary": "围绕品牌概览"},
            "parent_artifact_id": "parent-artifact-1",
            "narrative": {"summary": "结论", "findings": []},
            "blocks": [{"block_type": "markdown", "title": "说明", "content": "内容"}],
            "availability": {"blocks": {"status": "complete", "reason_codes": []}},
            "limitations": [],
            "methodology_input": {
                "data_as_of": "2026-01-15T12:00:00",
                "source_names": ["DataTap"],
                "notes": [],
            },
        }


def assemble_insight_payload(model_input: InsightBoardV1Input) -> dict[str, Any]:
    """模型输入 → 完整 insight_board_v1 发布 payload（无 canonical）。"""
    payload = {
        "schema_version": "insight_board_v1",
        "module": model_input.module,
        "data_status": _derive_data_status(
            model_input.availability, InsightBoardV1.REQUIRED_SECTIONS
        ),
        "availability": {
            section: entry.model_dump(mode="json")
            for section, entry in model_input.availability.items()
        },
        "limitations": [item.model_dump(mode="json") for item in model_input.limitations],
        "methodology": model_input.methodology_input.model_dump(mode="json"),
        "title": model_input.title,
        "scope": model_input.scope.model_dump(mode="json"),
        "parent_artifact_id": model_input.parent_artifact_id,
        "parent_artifact_version_id": model_input.parent_artifact_version_id,
        "narrative": model_input.narrative.model_dump(mode="json"),
        "data": [block.model_dump(mode="json") for block in model_input.blocks],
    }
    return payload


__all__ = [
    "InsightBoardV1Input",
    "InsightMethodologyInput",
    "assemble_insight_payload",
]
