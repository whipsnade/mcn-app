"""Direct Artifact Skill 模型输入 DTO（提交 1：模型输入契约 + 服务器组装边界）。

背景（真实 UAT Scenario 2 根因）：模型提交的 payload 被直接当完整
``BrandReportV3`` 发布契约校验，模型被迫手写 ``schema_version`` /
``module`` / ``data_status`` / ``methodology`` / ``canonical_data`` /
``field_lineage``——其中 canonical/lineage 必须精确覆盖 ``data`` 全部
叶子，模型几乎不可能手写成功（80 次调用全部失败）。

本包把「模型输入」与「发布 payload」解耦：

- 每个支持直接提交的 Artifact 类型对应一个严格模型输入 DTO
  （``extra="forbid"`` + ``frozen=True``，只收业务字段）；
- :func:`assemble_model_payload` 在服务器侧确定性组装完整发布 payload：
  推导 ``data_status``、组装 ``methodology``、用 :func:`publish_canonical`
  生成覆盖全部 ``data`` 叶子的 canonical/lineage（模型不再手写）；
- :func:`model_input_contract` 为后续提交提供单一事实源（JSON Schema +
  合法示例），供 Skill 文档/上下文注入使用。

约定：模型输入禁止出现任何服务器字段；DTO 直接复用 ``payloads/`` 下的
类型，不复制定义。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.agent_artifacts.model_inputs.brand import BrandReportV3Input, assemble_brand_payload
from app.agent_artifacts.model_inputs.campaign import (
    CampaignReportV3Input,
    assemble_campaign_payload,
)
from app.agent_artifacts.model_inputs.insight import (
    InsightBoardV1Input,
    assemble_insight_payload,
)
from app.agent_artifacts.model_inputs.kol_selection import (
    KolSelectionV3Input,
    assemble_kol_selection_payload,
)

#: 模型输入 DTO 契约版本（Skill 文档随版本演进，不随 payload schema 漂移）。
INPUT_SCHEMA_VERSION = "direct_model_input_v1"

#: Artifact 类型 → 模型输入 DTO（单 artifact_type 单一输入契约）。
#: campaign_report_v2/v3 共享同一 DTO，schema_version 由 assemble 参数化。
MODEL_INPUT_BY_ARTIFACT_TYPE: dict[str, type[BaseModel]] = {
    "brand_report_v3": BrandReportV3Input,
    "campaign_report_v2": CampaignReportV3Input,
    "campaign_report_v3": CampaignReportV3Input,
    "kol_selection_v3": KolSelectionV3Input,
    "insight_board_v1": InsightBoardV1Input,
}

#: 服务器字段：模型输入中出现即结构化拒绝（server_owned_field_rejected）。
SERVER_OWNED_PAYLOAD_KEYS = frozenset(
    {"schema_version", "module", "data_status", "canonical_data", "field_lineage"}
)


def assemble_model_payload(artifact_type: str, model_input: BaseModel) -> dict[str, Any]:
    """按 artifact_type 分发：模型输入 → 完整强类型发布 payload。"""
    if artifact_type == "brand_report_v3":
        return assemble_brand_payload(model_input)  # type: ignore[arg-type]
    if artifact_type in {"campaign_report_v2", "campaign_report_v3"}:
        return assemble_campaign_payload(model_input, schema_version=artifact_type)  # type: ignore[arg-type]
    if artifact_type == "kol_selection_v3":
        return assemble_kol_selection_payload(model_input)  # type: ignore[arg-type]
    if artifact_type == "insight_board_v1":
        return assemble_insight_payload(model_input)  # type: ignore[arg-type]
    raise ValueError(f"no direct model input assembler for artifact type {artifact_type!r}")


def model_input_contract(artifact_type: str) -> dict[str, Any]:
    """返回该 Artifact 类型的模型输入契约（后续提交注入 Skill 上下文）。

    ``model_input_schema`` 直接取 DTO 类 ``model_json_schema()``
    （validation 模式单一事实源，不手写副本）；``concise_example`` 是 DTO
    类方法生成的合法最小示例。``required_tools`` 与发布期望随契约一起下发，
    模型据此知道提交后如何进入发布链路。
    """
    dto = MODEL_INPUT_BY_ARTIFACT_TYPE[artifact_type]
    return {
        "artifact_type": artifact_type,
        "input_schema_version": INPUT_SCHEMA_VERSION,
        "model_input_schema": dto.model_json_schema(),
        "concise_example": dto.concise_example(),
        "required_tools": ["build_artifact_draft", "publish_artifacts"],
        "publication_expectations": {
            "via": "publish_artifacts",
            "same_version_bi_excel": True,
        },
    }


__all__ = [
    "INPUT_SCHEMA_VERSION",
    "MODEL_INPUT_BY_ARTIFACT_TYPE",
    "SERVER_OWNED_PAYLOAD_KEYS",
    "BrandReportV3Input",
    "CampaignReportV3Input",
    "InsightBoardV1Input",
    "KolSelectionV3Input",
    "assemble_brand_payload",
    "assemble_campaign_payload",
    "assemble_insight_payload",
    "assemble_kol_selection_payload",
    "assemble_model_payload",
    "model_input_contract",
]
