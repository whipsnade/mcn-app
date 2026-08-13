"""模型输入 DTO 与服务器组装测试（提交 1：direct model input contract）。

核心断言：
- 合法模型输入（不含服务器字段）→ assemble → 发布 payload 类校验通过；
- brand/campaign canonical_data/field_lineage 精确覆盖全部 data 叶子且恒等映射；
- data_status 按 REQUIRED_SECTIONS availability 推导；
- 服务器字段出现在输入中 → extra_forbid 拒绝；
- kol/insight 组装结果不携带 canonical；
- concise_example 对每个 artifact_type 都是合法模型输入且不含保留键；
- model_input_contract 的 schema 与 DTO 类 model_json_schema 完全一致。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agent_artifacts.canonical import model_direct_lineage_context, walk_data_leaves
from app.agent_artifacts.model_inputs import (
    INPUT_SCHEMA_VERSION,
    MODEL_INPUT_BY_ARTIFACT_TYPE,
    SERVER_OWNED_PAYLOAD_KEYS,
    assemble_model_payload,
    model_input_contract,
)
from app.agent_artifacts.model_inputs.brand import BrandReportV3Input
from app.agent_artifacts.model_inputs.campaign import CampaignReportV3Input
from app.agent_artifacts.model_inputs.insight import InsightBoardV1Input
from app.agent_artifacts.model_inputs.kol_selection import KolSelectionV3Input
from app.agent_artifacts.payloads import (
    BrandReportV3,
    CampaignReportV2,
    CampaignReportV3,
    InsightBoardV1,
    KolSelectionV3,
)

from tests.agent_artifacts.test_payloads import (
    build_brand_dict,
    build_campaign_dict,
    build_kol_selection_dict,
)

PERIOD = {"start": "2026-01-01", "end": "2026-01-31", "timezone": "Asia/Shanghai"}
METHODOLOGY_INPUT = {
    "data_as_of": "2026-01-15T12:00:00",
    "source_names": ["DataTap"],
    "notes": [],
}


def build_brand_model_input() -> dict:
    """从完整 brand fixture 取业务字段子集（去掉服务器字段与 canonical）。"""
    d = build_brand_dict()
    return {
        "scope": d["scope"],
        "data": d["data"],
        "narrative": d["narrative"],
        "availability": d["availability"],
        "limitations": d["limitations"],
        "methodology_input": {
            "data_as_of": d["methodology"]["data_as_of"],
            "source_names": d["methodology"]["source_names"],
            "notes": d["methodology"]["notes"],
        },
    }


def build_campaign_model_input() -> dict:
    d = build_campaign_dict()
    return {
        "scope": d["scope"],
        "data": d["data"],
        "narrative": d["narrative"],
        "availability": d["availability"],
        "limitations": d["limitations"],
        "methodology_input": {
            "data_as_of": d["methodology"]["data_as_of"],
            "source_names": d["methodology"]["source_names"],
            "notes": d["methodology"]["notes"],
        },
    }


def build_kol_model_input() -> dict:
    d = build_kol_selection_dict()
    return {
        "scope": d["scope"],
        "data": d["data"],
        "narrative": d["narrative"],
        "availability": d["availability"],
        "limitations": d["limitations"],
        "methodology_input": {
            "data_as_of": d["methodology"]["data_as_of"],
            "source_names": d["methodology"]["source_names"],
            "notes": d["methodology"]["notes"],
        },
    }


def build_insight_model_input() -> dict:
    return {
        "title": "品牌钻取",
        "scope": {"summary": "围绕品牌概览", "period": PERIOD, "platforms": ["xiaohongshu"]},
        "parent_artifact_id": "art-1",
        "narrative": {"summary": "摘要", "findings": []},
        "blocks": [
            {
                "block_type": "metric_grid",
                "title": "概览",
                "cards": [{"key": "volume", "label": "声量", "value": 1000}],
            },
            {"block_type": "markdown", "title": "说明", "content": "文字"},
        ],
        "availability": {"blocks": {"status": "complete", "reason_codes": []}},
        "limitations": [],
        "methodology_input": METHODOLOGY_INPUT,
    }


def _assert_canonical_exact(instance) -> None:
    """canonical path 集合 == data 全部叶子；field_lineage 恒等映射。"""
    data_dump = instance.data.model_dump(mode="json")
    leaves = {path for path, _value in walk_data_leaves(data_dump)}
    assert {field.path for field in instance.canonical_data} == leaves
    assert set(instance.field_lineage) == leaves
    for path in leaves:
        assert tuple(instance.field_lineage[path]) == (path,)


def _validate_payload(payload_cls, payload):
    """与真实链路 ``validate_revision_payload(direct_model_payload=True)`` 等价：
    direct 模型提交的 canonical 数值字段不要求 Evidence，需在
    ``model_direct_lineage_context`` 下校验（组装同理）。"""
    with model_direct_lineage_context():
        return payload_cls.model_validate(payload)


# --------------------------------------------------------------------------- #
# brand
# --------------------------------------------------------------------------- #


def test_brand_assemble_valid_and_canonical_exact() -> None:
    model_input = BrandReportV3Input.model_validate(build_brand_model_input())
    payload = assemble_model_payload("brand_report_v3", model_input)
    instance = _validate_payload(BrandReportV3, payload)
    assert instance.schema_version == "brand_report_v3"
    assert instance.module == "brand"
    assert payload["methodology"]["data_as_of"].startswith("2026-01-15T12:00:00")
    _assert_canonical_exact(instance)


def test_brand_data_status_derivation() -> None:
    full = build_brand_model_input()
    payload = assemble_model_payload("brand_report_v3", BrandReportV3Input.model_validate(full))
    assert payload["data_status"] == "complete"
    _validate_payload(BrandReportV3, payload)

    partial = dict(full)
    partial["availability"] = dict(full["availability"])
    partial["availability"]["overview"] = {"status": "partial", "reason_codes": ["missing"]}
    partial["limitations"] = [
        {"code": "L_VOLUME", "message": "声量部分缺失", "affected_paths": ["overview.total_volume"]}
    ]
    payload2 = assemble_model_payload(
        "brand_report_v3", BrandReportV3Input.model_validate(partial)
    )
    assert payload2["data_status"] == "restricted"
    instance = _validate_payload(BrandReportV3, payload2)
    assert instance.data_status == "restricted"
    # partial 章节的叶子在 canonical 中标记为 partial。
    overview = next(field for field in instance.canonical_data if field.path == "/data/overview/total_volume")
    assert overview.availability == "partial"


def test_brand_rejects_server_owned_fields() -> None:
    d = build_brand_model_input()
    d["schema_version"] = "brand_report_v3"
    with pytest.raises(ValidationError):
        BrandReportV3Input.model_validate(d)
    d2 = build_brand_model_input()
    d2["canonical_data"] = []
    with pytest.raises(ValidationError):
        BrandReportV3Input.model_validate(d2)


# --------------------------------------------------------------------------- #
# campaign
# --------------------------------------------------------------------------- #


def test_campaign_assemble_valid_for_both_schema_versions() -> None:
    model_input = CampaignReportV3Input.model_validate(build_campaign_model_input())
    for schema_version, payload_cls in (
        ("campaign_report_v2", CampaignReportV2),
        ("campaign_report_v3", CampaignReportV3),
    ):
        payload = assemble_model_payload(schema_version, model_input)
        instance = _validate_payload(payload_cls, payload)
        assert instance.schema_version == schema_version
        assert instance.module == "campaign"
        _assert_canonical_exact(instance)


# --------------------------------------------------------------------------- #
# kol / insight
# --------------------------------------------------------------------------- #


def test_kol_selection_assemble_valid_without_canonical() -> None:
    model_input = KolSelectionV3Input.model_validate(build_kol_model_input())
    payload = assemble_model_payload("kol_selection_v3", model_input)
    assert "canonical_data" not in payload
    assert "field_lineage" not in payload
    instance = _validate_payload(KolSelectionV3, payload)
    assert instance.module == "kol"
    assert instance.data_status == "complete"


def test_insight_assemble_valid_without_canonical() -> None:
    model_input = InsightBoardV1Input.model_validate(build_insight_model_input())
    payload = assemble_model_payload("insight_board_v1", model_input)
    assert "canonical_data" not in payload
    assert "field_lineage" not in payload
    instance = _validate_payload(InsightBoardV1, payload)
    assert instance.schema_version == "insight_board_v1"
    assert instance.module == "brand"
    assert len(instance.data) == 2


# --------------------------------------------------------------------------- #
# concise_example / model_input_contract
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("artifact_type", sorted(MODEL_INPUT_BY_ARTIFACT_TYPE))
def test_concise_example_is_valid_model_input(artifact_type: str) -> None:
    dto = MODEL_INPUT_BY_ARTIFACT_TYPE[artifact_type]
    example = dto.concise_example()
    dto.model_validate(example)
    assert not (set(example) & SERVER_OWNED_PAYLOAD_KEYS)
    # assemble 后也必须能过发布 payload 校验（示例是可用输入的证据）。
    assembled = assemble_model_payload(artifact_type, dto.model_validate(example))
    from app.agent_artifacts.payloads import TYPED_PAYLOAD_BY_SCHEMA

    _validate_payload(TYPED_PAYLOAD_BY_SCHEMA[artifact_type], assembled)


@pytest.mark.parametrize("artifact_type", sorted(MODEL_INPUT_BY_ARTIFACT_TYPE))
def test_model_input_contract_schema_is_single_source_of_truth(artifact_type: str) -> None:
    dto = MODEL_INPUT_BY_ARTIFACT_TYPE[artifact_type]
    contract = model_input_contract(artifact_type)
    assert contract["artifact_type"] == artifact_type
    assert contract["input_schema_version"] == INPUT_SCHEMA_VERSION
    assert contract["model_input_schema"] == dto.model_json_schema()
    assert contract["required_tools"] == ["build_artifact_draft", "publish_artifacts"]
    assert contract["publication_expectations"] == {
        "via": "publish_artifacts",
        "same_version_bi_excel": True,
    }


def test_insight_module_is_server_owned() -> None:
    """module 是服务器拥有字段：模型输入必须拒绝 module 键，concise_example 不含之。"""
    example = InsightBoardV1Input.concise_example()
    assert "module" not in example
    with pytest.raises(ValidationError):
        InsightBoardV1Input.model_validate({**build_insight_model_input(), "module": "brand"})
