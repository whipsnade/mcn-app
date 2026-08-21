"""analysis_report_v1 发布边界与标准 Artifact 兼容测试。"""

from __future__ import annotations

import copy

import pytest

from app.agent_artifacts.model_inputs.analysis_report import (
    AnalysisReportV1Input,
    assemble_analysis_report_payload,
)
from app.agent_artifacts.payloads import AnalysisReportV1, TYPED_PAYLOAD_BY_SCHEMA
from app.agent_artifacts.validation import ArtifactPayloadInvalid, ArtifactPayloadValidator

from tests.agent_artifacts.test_analysis_report_payload import _base_input
from tests.agent_artifacts.test_payloads import build_brand_dict


def _report_payload() -> dict:
    model_input = AnalysisReportV1Input.model_validate(_base_input())
    return assemble_analysis_report_payload(model_input)


def test_analysis_report_is_registered_and_publishes_as_report_module() -> None:
    assert TYPED_PAYLOAD_BY_SCHEMA["analysis_report_v1"] is AnalysisReportV1
    normalized = ArtifactPayloadValidator.validate_new_draft(
        module="report",
        schema_version="analysis_report_v1",
        artifact_type="analysis_report_v1",
        business_fields={"scope": {"brand": "示例品牌"}, "title": "跨平台营销分析"},
        payload=_report_payload(),
    )
    assert normalized["module"] == "report"
    assert normalized["schema_version"] == "analysis_report_v1"
    assert "canonical_data" not in normalized


def test_analysis_report_rejects_naked_or_wrong_module_identity() -> None:
    with pytest.raises(ArtifactPayloadInvalid):
        ArtifactPayloadValidator.validate_new_draft(
            module="report",
            schema_version="analysis_report_v1",
            artifact_type="analysis_report_v1",
            business_fields={},
            payload=_report_payload(),
        )
    with pytest.raises(ArtifactPayloadInvalid):
        ArtifactPayloadValidator.validate_new_draft(
            module="brand",
            schema_version="analysis_report_v1",
            artifact_type="analysis_report_v1",
            business_fields={"brand": "示例品牌"},
            payload=_report_payload(),
        )


def test_standard_brand_payload_mapping_still_accepts_existing_contract() -> None:
    payload = copy.deepcopy(build_brand_dict())
    normalized = ArtifactPayloadValidator.validate_revision_payload(
        module="brand",
        schema_version="brand_report_v3",
        artifact_type="brand_report_v3",
        payload=payload,
    )
    assert normalized["schema_version"] == "brand_report_v3"
    assert normalized["module"] == "brand"
