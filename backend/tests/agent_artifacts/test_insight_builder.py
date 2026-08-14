"""``insight_board_v1`` Draft builder 测试（H5：开放式钻取看板收口）。

builder 是纯确定性组装：输入「已解析取值的板块 + 数字级 lineage」，输出
过强类型校验的 :class:`DraftBuildResult`。取值/归属/发布校验在工具层
（``tests/agent_runtime/tools/test_insight_builder.py``）。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agent_artifacts.builders.common import DraftBuildError
from app.agent_artifacts.builders.insight import (
    SCHEMA_VERSION,
    ResolvedBlock,
    ResolvedLineage,
    build_insight_draft,
)
from app.agent_artifacts.lineage import required_numeric_pointers
from app.agent_artifacts.payloads.insight import InsightBoardV1

PARENT_ARTIFACT_ID = "parent-artifact-1"
PARENT_VERSION_ID = "parent-version-1"


def _evidence_source(evidence_id: str = "ev-1", source_path: str = "/0/声量") -> dict[str, Any]:
    return {"source_type": "evidence", "evidence_id": evidence_id, "source_path": source_path}


def _eight_blocks() -> list[ResolvedBlock]:
    """8 种 Block 全型；数字叶子一律附 lineage（路径与 payload 对齐）。"""
    return [
        ResolvedBlock(
            block={
                "block_type": "metric_grid",
                "title": "核心指标",
                "cards": [{"key": "total_volume", "label": "声量", "value": 100, "unit": "条"}],
            },
            lineage=[ResolvedLineage("/data/0/cards/0/value", [_evidence_source()])],
        ),
        ResolvedBlock(
            block={
                "block_type": "table",
                "title": "分平台",
                "columns": ["平台", "声量"],
                "rows": [["小红书", 1000]],
            },
            lineage=[ResolvedLineage("/data/1/rows/0/1", [_evidence_source()])],
        ),
        ResolvedBlock(
            block={
                "block_type": "bar_chart",
                "title": "平台对比",
                "categories": ["小红书", "抖音"],
                "series": [{"name": "声量", "values": [10, 20]}],
            },
            lineage=[
                ResolvedLineage("/data/2/series/0/values/0", [_evidence_source()]),
                ResolvedLineage("/data/2/series/0/values/1", [_evidence_source()]),
            ],
        ),
        ResolvedBlock(
            block={
                "block_type": "line_chart",
                "title": "趋势",
                "x_labels": ["2026-07-01"],
                "series": [{"name": "声量", "values": [1.5]}],
            },
            lineage=[ResolvedLineage("/data/3/series/0/values/0", [_evidence_source()])],
        ),
        ResolvedBlock(
            block={
                "block_type": "pie_chart",
                "title": "情感占比",
                "slices": [{"name": "正面", "value": 42.0}],
            },
            lineage=[
                ResolvedLineage(
                    "/data/4/slices/0/value",
                    [_evidence_source()],
                    {
                        "tool_call_id": "call-1",
                        "method": "calculate_expression",
                        "input_paths": ["/0/声量"],
                    },
                )
            ],
        ),
        ResolvedBlock(
            block={"block_type": "markdown", "title": "结论", "content": "声量集中在小红书。"},
            lineage=[],
        ),
        ResolvedBlock(
            block={
                "block_type": "timeline",
                "title": "节奏",
                "items": [{"date": "2026-07-01", "title": "上新", "description": ""}],
            },
            lineage=[],
        ),
        ResolvedBlock(
            block={
                "block_type": "references",
                "title": "来源",
                "items": [{"label": "原帖", "url": "https://example.com/p/1"}],
            },
            lineage=[],
        ),
    ]


def _build(**overrides: Any):
    kwargs: dict[str, Any] = {
        "question": "分平台声量对比？",
        "title": "分平台钻取",
        "module": "brand",
        "scope": {"summary": "按平台钻取", "brand": "瑞幸咖啡"},
        "parent_artifact_id": PARENT_ARTIFACT_ID,
        "parent_artifact_version_id": PARENT_VERSION_ID,
        "blocks": _eight_blocks(),
    }
    kwargs.update(overrides)
    return build_insight_draft(**kwargs)


def test_build_insight_draft_all_eight_block_types() -> None:
    result = _build()

    assert result.module == "insight"
    assert result.schema_version == SCHEMA_VERSION == "insight_board_v1"
    assert result.artifact_type == "insight_board_v1"
    # 稳定 key 业务字段：parent version + question（keys.py insight 规则）。
    assert result.business_fields == {
        "parent_artifact_version_id": PARENT_VERSION_ID,
        "question": "分平台声量对比？",
    }
    assert result.parent_artifact_id == PARENT_ARTIFACT_ID
    assert result.parent_artifact_version_id == PARENT_VERSION_ID

    payload = result.payload
    InsightBoardV1.model_validate(payload)
    assert payload["data_status"] == "complete"
    assert payload["availability"] == {"blocks": {"status": "complete", "reason_codes": []}}
    assert payload["limitations"] == []
    assert payload["parent_artifact_id"] == PARENT_ARTIFACT_ID
    assert [block["block_type"] for block in payload["data"]] == [
        "metric_grid",
        "table",
        "bar_chart",
        "line_chart",
        "pie_chart",
        "markdown",
        "timeline",
        "references",
    ]

    # 数字级 lineage 全覆盖：emit 的 refs 恰好等于必选 numeric 指针集合。
    required = required_numeric_pointers(payload)
    emitted = {ref["artifact_path"] for ref in result.evidence_refs}
    assert emitted == required
    assert len(emitted) == len(result.evidence_refs)  # artifact_path 唯一
    pie_ref = next(
        ref for ref in result.evidence_refs if ref["artifact_path"] == "/data/4/slices/0/value"
    )
    assert pie_ref["derivation"]["tool_call_id"] == "call-1"


def test_build_insight_draft_narrative_fallback() -> None:
    result = _build(narrative=None)
    narrative = result.payload["narrative"]
    assert "分平台声量对比" in narrative["summary"]
    assert narrative["findings"] == []


def test_build_insight_draft_model_narrative_passthrough() -> None:
    result = _build(
        narrative={
            "summary": "小红书声量占绝对主导。",
            "findings": [
                {
                    "title": "平台集中",
                    "detail": "小红书声量 1000。",
                    "supporting_paths": ["data.1.rows.0.1"],
                }
            ],
        }
    )
    assert result.payload["narrative"]["summary"] == "小红书声量占绝对主导。"


def test_build_insight_draft_invalid_narrative_rejected() -> None:
    # supporting_paths 必须指向 data 内真实路径。
    with pytest.raises(DraftBuildError):
        _build(
            narrative={
                "summary": "s",
                "findings": [{"title": "t", "detail": "d", "supporting_paths": ["data.bogus"]}],
            }
        )
    # 多余字段（模型常见错误）同样拒绝。
    with pytest.raises(DraftBuildError):
        _build(narrative={"summary": "s", "findings": [], "extra": 1})


def test_build_insight_draft_rejects_lineage_path_outside_payload() -> None:
    blocks = _eight_blocks()
    blocks[0] = ResolvedBlock(
        block=blocks[0].block,
        lineage=[ResolvedLineage("/data/0/cards/9/value", [_evidence_source()])],
    )
    with pytest.raises(DraftBuildError):
        _build(blocks=blocks)


def test_build_insight_draft_rejects_duplicate_lineage_path() -> None:
    blocks = _eight_blocks()
    blocks[0] = ResolvedBlock(
        block=blocks[0].block,
        lineage=[
            ResolvedLineage("/data/0/cards/0/value", [_evidence_source()]),
            ResolvedLineage("/data/0/cards/0/value", [_evidence_source("ev-2")]),
        ],
    )
    with pytest.raises(DraftBuildError):
        _build(blocks=blocks)


def test_build_insight_draft_invalid_scope_rejected() -> None:
    # InsightScope extra="forbid"：模型编造字段字段级拒绝。
    with pytest.raises(DraftBuildError):
        _build(scope={"summary": "s", "bogus_field": 1})


def test_build_insight_draft_invalid_module_rejected() -> None:
    with pytest.raises(DraftBuildError):
        _build(module="insight")


def test_build_insight_draft_empty_blocks_rejected() -> None:
    # 空看板没有交付价值：工具层 min_length=1 之外 builder 也兜底。
    with pytest.raises(DraftBuildError):
        _build(blocks=[])
