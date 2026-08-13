"""合法强类型 Artifact payload 测试工厂（v3 加固 A5）。

A5 起 Draft 写入必须经过强类型校验，并保存 ``model_dump(mode="json")``
标准化形态。这里的工厂直接经 Pydantic 构建再标准化 dump，保证「输入即
存储形态」——断言 Revision payload 相等时不受默认值填充/日期序列化影响。

``insight_payload`` 无必需数字叶子（markdown block），lineage 闭包为空，
发布/送审无需建 Evidence；``insight_metric_payload`` 带一个必需数字叶子，
用于 lineage 相关测试。
"""

from __future__ import annotations

import json
from typing import Any

from app.agent_artifacts.payloads import BrandReportV3, CampaignReportV2, InsightBoardV1

from tests.agent_artifacts.test_payloads import build_brand_dict, build_campaign_dict


def insight_payload(
    *,
    title: str = "钻取结论",
    parent_artifact_id: str = "parent-artifact-1",
    blocks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """最小合法 insight_board_v1（无必需数字叶子，lineage 空闭包）。"""
    payload: dict[str, Any] = {
        "schema_version": "insight_board_v1",
        "module": "brand",
        "data_status": "complete",
        "availability": {"blocks": {"status": "complete", "reason_codes": []}},
        "limitations": [],
        "methodology": {
            "data_as_of": "2026-01-15T12:00:00",
            "source_names": ["DataTap"],
            "notes": [],
        },
        "title": title,
        "scope": {"summary": "钻取"},
        "parent_artifact_id": parent_artifact_id,
        "narrative": {"summary": "结论", "findings": []},
        "data": blocks
        if blocks is not None
        else [{"block_type": "markdown", "title": "说明", "content": "内容"}],
    }
    return InsightBoardV1.model_validate(payload).model_dump(mode="json")


def insight_metric_payload(*, value: int = 100) -> dict[str, Any]:
    """带一个必需数字叶子（metric_grid card value）的合法 insight_board_v1。

    必需 lineage 指针为 ``/data/0/cards/0/value``。
    """
    return insight_payload(
        blocks=[
            {
                "block_type": "metric_grid",
                "title": "指标",
                "cards": [{"key": "total_volume", "label": "声量", "value": value}],
            }
        ]
    )


def insight_metric_refs(evidence_id: str, *, source_path: str = "/0/声量") -> list[dict[str, Any]]:
    """``insight_metric_payload`` 唯一数字叶子对应的 evidence_refs。"""
    return [
        {
            "artifact_path": "/data/0/cards/0/value",
            "sources": [
                {"source_type": "evidence", "evidence_id": evidence_id, "source_path": source_path}
            ],
        }
    ]


def brand_payload() -> dict[str, Any]:
    """合法 brand_report_v3（标准化 dump；复用 test_payloads 的完整字典）。"""
    return BrandReportV3.model_validate(build_brand_dict()).model_dump(mode="json")


def brand_model_input() -> dict[str, Any]:
    """合法品牌模型输入（build_artifact_draft 的 payload 形态，提交 3）。

    不含 schema_version/module/data_status/canonical_data/field_lineage 等
    服务器字段；服务器按 model_input_contract 组装为完整 brand_report_v3。
    ``data`` 里的 date 对象经 JSON round-trip 转 ISO 字符串（跨进程传输要求
    JSON 安全，与 DTO 的 model_dump(mode="json") 形态一致）。
    """
    d = build_brand_dict()
    model_input = {
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
    return json.loads(json.dumps(model_input, ensure_ascii=False, default=str))


def campaign_payload() -> dict[str, Any]:
    """合法 campaign_report_v2（标准化 dump；复用 test_payloads 的完整字典）。"""
    return CampaignReportV2.model_validate(build_campaign_dict()).model_dump(mode="json")


__all__ = [
    "brand_model_input",
    "brand_payload",
    "campaign_payload",
    "insight_metric_payload",
    "insight_metric_refs",
    "insight_payload",
]
