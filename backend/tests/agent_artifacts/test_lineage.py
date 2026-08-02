"""字段级 Evidence Lineage 校验器测试（设计文档 §10.4 / Task 11）。

覆盖：
1. RFC 6901 JSON Pointer 语法校验与解析（artifact_path / source_path 指向不存在的
   payload 位置被拒绝）；
2. Evidence 来源归属：evidence_id 必须存在且属于当前 user+session；
3. Artifact 来源：artifact_version_id 属于当前 session，且 source_path 在该版本
   payload 中存在；
4. 确定性 derivation：tool_call_id 必须指向 settled 的内部零积分工具调用，
   input_paths 必须在来源 payload 内解析；
5. 递归闭包：artifact 来源递归展开到 Evidence；无 Evidence 基座拒绝；环拒绝；
6. 必选 numeric 覆盖：insight_board_v1 规则——metric/series/table 数字要 lineage；
   日期/版本/评分权重/布局元数据除外；
7. 冻结快照：FrozenLineage 自包含、稳定、包含完整传递闭包。

生产 loader 用 MySQL（DbLineageLoader），本文件大部分用例注入内存 loader。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.agent_artifacts.lineage import (
    ArtifactVersionRecord,
    DbLineageLoader,
    EvidenceRecord,
    LineageError,
    LineageOwner,
    ToolCallRecord,
    validate_and_freeze_lineage,
)
from app.agent_artifacts.schemas import (
    ArtifactSource,
    DerivationRef,
    EvidenceSource,
    FrozenEvidenceSource,
    FrozenLineage,
    LineageRef,
    validate_json_pointer,
)
from app.agent_runtime.models import (
    AgentRun,
    AgentRunAttempt,
    AgentSession,
    AgentStep,
    AgentToolCall,
    EvidenceItem,
)

OWNER = LineageOwner(user_id="u-1", session_id="s-1")

# Evidence 原始 payload：行数组，字段级 source_path 形如 /0/声量。
EVIDENCE_PAYLOAD = [
    {"声量": 100000, "正面声量数": 60000, "负面声量数": 40000},
]

# insight_board_v1 中要求 lineage 的全部数值叶子指针。
INSIGHT_REQUIRED_PATHS = (
    "/data/blocks/0/cards/0/value",
    "/data/blocks/0/cards/1/value",
    "/data/blocks/1/rows/0/1",
    "/data/blocks/1/rows/1/1",
    "/data/blocks/2/series/0/values/0",
    "/data/blocks/2/series/0/values/1",
    "/data/blocks/3/slices/0/value",
    "/data/blocks/3/slices/1/value",
)


# ---------------------------------------------------------------------------
# 内存 loader
# ---------------------------------------------------------------------------


class MemoryLoader:
    """测试用内存 loader：注入 Evidence / Artifact Version / Tool Call 记录。"""

    def __init__(self) -> None:
        self.evidence: dict[str, dict] = {}
        self.artifact_versions: dict[str, dict] = {}
        self.tool_calls: dict[str, dict] = {}

    def add_evidence(self, evidence_id: str, session_id: str, payload: object) -> None:
        self.evidence[evidence_id] = {
            "session_id": session_id,
            "payload": payload,
            "payload_hash": f"ph-{evidence_id}",
        }

    def add_artifact_version(
        self, version_id: str, session_id: str, payload: object, evidence_refs: list[dict]
    ) -> None:
        self.artifact_versions[version_id] = {
            "session_id": session_id,
            "payload": payload,
            "evidence_refs": evidence_refs,
        }

    def add_tool_call(
        self, tool_call_id: str, session_id: str, *, service: str = "internal", status: str = "settled"
    ) -> None:
        self.tool_calls[tool_call_id] = {
            "session_id": session_id,
            "service": service,
            "status": status,
        }

    async def load_evidence(self, evidence_id: str) -> EvidenceRecord | None:
        row = self.evidence.get(evidence_id)
        if row is None:
            return None
        return EvidenceRecord(
            id=evidence_id,
            session_id=row["session_id"],
            raw_payload=row["payload"],
            payload_hash=row["payload_hash"],
        )

    async def load_artifact_version(self, version_id: str) -> ArtifactVersionRecord | None:
        row = self.artifact_versions.get(version_id)
        if row is None:
            return None
        return ArtifactVersionRecord(
            id=version_id,
            session_id=row["session_id"],
            payload=row["payload"],
            evidence_refs=row["evidence_refs"],
        )

    async def load_tool_call(self, tool_call_id: str) -> ToolCallRecord | None:
        row = self.tool_calls.get(tool_call_id)
        if row is None:
            return None
        return ToolCallRecord(
            id=tool_call_id,
            session_id=row["session_id"],
            service=row["service"],
            status=row["status"],
        )


def _ref(path: str) -> LineageRef:
    return LineageRef(
        artifact_path=path,
        sources=(EvidenceSource(evidence_id="ev-1", source_path="/0/声量"),),
    )


def _insight_payload() -> dict:
    return {
        "schema_version": "insight_board_v1",
        "data": {
            "blocks": [
                {
                    "block_type": "metric_grid",
                    "title": "概览",
                    "cards": [
                        {"key": "total_volume", "label": "总声量", "value": 100000},
                        {"key": "positive_share", "label": "正声占比", "value": 0.6},
                    ],
                },
                {
                    "block_type": "table",
                    "title": "平台",
                    "columns": ["platform", "volume"],
                    "rows": [["xiaohongshu", 40000], ["douyin", 60000]],
                },
                {
                    "block_type": "line_chart",
                    "title": "趋势",
                    "x_labels": ["周一", "周二"],
                    "series": [{"name": "声量", "values": [50000.0, 60000.0]}],
                },
                {
                    "block_type": "pie_chart",
                    "title": "占比",
                    "slices": [{"name": "小红书", "value": 0.4}, {"name": "抖音", "value": 0.6}],
                },
                {
                    "block_type": "timeline",
                    "title": "里程碑",
                    "items": [{"date": "2026-01-01", "title": "发布", "description": ""}],
                },
            ]
        },
    }


# ---------------------------------------------------------------------------
# 1. JSON Pointer 语法与解析
# ---------------------------------------------------------------------------


def test_json_pointer_syntax_valid() -> None:
    assert validate_json_pointer("/data/overview/total_volume") == "/data/overview/total_volume"
    assert validate_json_pointer("/0/声量") == "/0/声量"
    assert validate_json_pointer("") == ""
    assert validate_json_pointer("/a~1b~0c") == "/a~1b~0c"


def test_json_pointer_escape_rule_enforced() -> None:
    # '~' 只能转义 0/1；裸 '~' 或 '~2' 属于非法语法。
    for bad in ("/a~", "/a~2b"):
        with pytest.raises(ValidationError):
            LineageRef(
                artifact_path=bad,
                sources=(EvidenceSource(evidence_id="e", source_path="/0/x"),),
            )


def test_json_pointer_must_start_with_slash() -> None:
    with pytest.raises(ValidationError):
        LineageRef(
            artifact_path="data/overview/total_volume",
            sources=(EvidenceSource(evidence_id="e", source_path="/0/x"),),
        )


def test_source_path_syntax_enforced() -> None:
    with pytest.raises(ValidationError):
        LineageRef(
            artifact_path="/data/a",
            sources=(EvidenceSource(evidence_id="e", source_path="0/声量"),),
        )


def test_ref_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        LineageRef(  # type: ignore[call-arg]
            artifact_path="/data/a",
            sources=(EvidenceSource(evidence_id="e", source_path="/0/x"),),
            bogus=1,
        )


async def test_artifact_path_resolves_in_payload() -> None:
    loader = MemoryLoader()
    loader.add_evidence("ev-1", "s-1", EVIDENCE_PAYLOAD)
    payload = {"schema_version": "insight_board_v1", "data": {"overview": {"total_volume": 100000}}}
    frozen = await validate_and_freeze_lineage(payload=payload, refs=[_ref("/data/overview/total_volume")], owner=OWNER, loader=loader)
    assert frozen.refs[0].artifact_path == "/data/overview/total_volume"


async def test_artifact_path_pointing_nowhere_rejected() -> None:
    loader = MemoryLoader()
    loader.add_evidence("ev-1", "s-1", EVIDENCE_PAYLOAD)
    payload = {"schema_version": "insight_board_v1", "data": {"overview": {"total_volume": 100000}}}
    with pytest.raises(LineageError) as exc_info:
        await validate_and_freeze_lineage(
            payload=payload,
            refs=[_ref("/data/overview/nope")],
            owner=OWNER,
            loader=loader,
        )
    assert exc_info.value.code == "pointer_not_found"


async def test_evidence_source_path_pointing_nowhere_rejected() -> None:
    loader = MemoryLoader()
    loader.add_evidence("ev-1", "s-1", EVIDENCE_PAYLOAD)
    payload = {"schema_version": "insight_board_v1", "data": {"overview": {"total_volume": 100000}}}
    refs = [
        LineageRef(
            artifact_path="/data/overview/total_volume",
            sources=(EvidenceSource(evidence_id="ev-1", source_path="/99/声量"),),
        )
    ]
    with pytest.raises(LineageError) as exc_info:
        await validate_and_freeze_lineage(payload=payload, refs=refs, owner=OWNER, loader=loader)
    assert exc_info.value.code == "evidence_source_path_not_found"


# ---------------------------------------------------------------------------
# 2. Evidence 来源归属
# ---------------------------------------------------------------------------


async def test_evidence_in_current_session_accepted() -> None:
    loader = MemoryLoader()
    loader.add_evidence("ev-1", "s-1", EVIDENCE_PAYLOAD)
    payload = {"schema_version": "insight_board_v1", "data": {"overview": {"total_volume": 100000}}}
    frozen = await validate_and_freeze_lineage(payload=payload, refs=[_ref("/data/overview/total_volume")], owner=OWNER, loader=loader)
    assert frozen.refs[0].sources == (
        FrozenEvidenceSource(evidence_id="ev-1", source_path="/0/声量", payload_hash="ph-ev-1"),
    )


async def test_cross_session_evidence_rejected() -> None:
    loader = MemoryLoader()
    loader.add_evidence("ev-1", "s-2", EVIDENCE_PAYLOAD)  # 属于另一个 session
    payload = {"schema_version": "insight_board_v1", "data": {"overview": {"total_volume": 100000}}}
    with pytest.raises(LineageError) as exc_info:
        await validate_and_freeze_lineage(payload=payload, refs=[_ref("/data/overview/total_volume")], owner=OWNER, loader=loader)
    assert exc_info.value.code == "evidence_not_owned"


async def test_nonexistent_evidence_rejected() -> None:
    loader = MemoryLoader()  # 未注册任何 evidence
    payload = {"schema_version": "insight_board_v1", "data": {"overview": {"total_volume": 100000}}}
    with pytest.raises(LineageError) as exc_info:
        await validate_and_freeze_lineage(payload=payload, refs=[_ref("/data/overview/total_volume")], owner=OWNER, loader=loader)
    assert exc_info.value.code == "evidence_not_found"


# ---------------------------------------------------------------------------
# 3. Artifact 来源
# ---------------------------------------------------------------------------


async def test_artifact_source_expands_to_evidence() -> None:
    loader = MemoryLoader()
    loader.add_evidence("ev-1", "s-1", EVIDENCE_PAYLOAD)
    loader.add_artifact_version(
        "va-1",
        "s-1",
        {"data": {"overview": {"total_volume": 100000}}},
        [_ref("/data/overview/total_volume").model_dump()],
    )
    payload = {"schema_version": "insight_board_v1", "data": {"grand_total": 100000}}
    refs = [
        LineageRef(
            artifact_path="/data/grand_total",
            sources=(ArtifactSource(artifact_version_id="va-1", source_path="/data/overview/total_volume"),),
        )
    ]
    frozen = await validate_and_freeze_lineage(payload=payload, refs=refs, owner=OWNER, loader=loader)
    assert frozen.refs[0].sources == (
        FrozenEvidenceSource(evidence_id="ev-1", source_path="/0/声量", payload_hash="ph-ev-1"),
    )


async def test_artifact_version_cross_session_rejected() -> None:
    loader = MemoryLoader()
    loader.add_evidence("ev-1", "s-1", EVIDENCE_PAYLOAD)
    loader.add_artifact_version(
        "va-1",
        "s-2",  # 另一个 session
        {"data": {"overview": {"total_volume": 100000}}},
        [_ref("/data/overview/total_volume").model_dump()],
    )
    payload = {"schema_version": "insight_board_v1", "data": {"grand_total": 100000}}
    refs = [
        LineageRef(
            artifact_path="/data/grand_total",
            sources=(ArtifactSource(artifact_version_id="va-1", source_path="/data/overview/total_volume"),),
        )
    ]
    with pytest.raises(LineageError) as exc_info:
        await validate_and_freeze_lineage(payload=payload, refs=refs, owner=OWNER, loader=loader)
    assert exc_info.value.code == "artifact_not_owned"


async def test_artifact_version_source_path_nowhere_rejected() -> None:
    loader = MemoryLoader()
    loader.add_artifact_version(
        "va-1",
        "s-1",
        {"data": {"other": 1}},
        [],
    )
    payload = {"schema_version": "insight_board_v1", "data": {"grand_total": 100000}}
    refs = [
        LineageRef(
            artifact_path="/data/grand_total",
            sources=(ArtifactSource(artifact_version_id="va-1", source_path="/data/missing"),),
        )
    ]
    with pytest.raises(LineageError) as exc_info:
        await validate_and_freeze_lineage(payload=payload, refs=refs, owner=OWNER, loader=loader)
    assert exc_info.value.code == "artifact_source_path_not_found"


async def test_artifact_version_not_found_rejected() -> None:
    loader = MemoryLoader()
    payload = {"schema_version": "insight_board_v1", "data": {"grand_total": 100000}}
    refs = [
        LineageRef(
            artifact_path="/data/grand_total",
            sources=(ArtifactSource(artifact_version_id="va-missing", source_path="/data/overview/total_volume"),),
        )
    ]
    with pytest.raises(LineageError) as exc_info:
        await validate_and_freeze_lineage(payload=payload, refs=refs, owner=OWNER, loader=loader)
    assert exc_info.value.code == "artifact_not_found"


# ---------------------------------------------------------------------------
# 4. 确定性 derivation
# ---------------------------------------------------------------------------


def _share_payload() -> dict:
    return {"schema_version": "insight_board_v1", "data": {"positive_share": 0.6}}


def _share_ref(tool_call_id: str = "tc-1", input_paths: tuple[str, ...] = ("/0/正面声量数", "/0/声量")) -> LineageRef:
    return LineageRef(
        artifact_path="/data/positive_share",
        sources=(
            EvidenceSource(evidence_id="ev-1", source_path="/0/正面声量数"),
            EvidenceSource(evidence_id="ev-1", source_path="/0/声量"),
        ),
        derivation=DerivationRef(tool_call_id=tool_call_id, method="divide", input_paths=input_paths),
    )


async def test_derivation_settled_internal_tool_call_accepted() -> None:
    loader = MemoryLoader()
    loader.add_evidence("ev-1", "s-1", EVIDENCE_PAYLOAD)
    loader.add_tool_call("tc-1", "s-1", service="internal", status="settled")
    frozen = await validate_and_freeze_lineage(payload=_share_payload(), refs=[_share_ref()], owner=OWNER, loader=loader)
    assert frozen.refs[0].derivation is not None
    assert frozen.refs[0].derivation.tool_call_id == "tc-1"


async def test_derivation_unsettled_tool_call_rejected() -> None:
    loader = MemoryLoader()
    loader.add_evidence("ev-1", "s-1", EVIDENCE_PAYLOAD)
    loader.add_tool_call("tc-1", "s-1", service="internal", status="running")
    with pytest.raises(LineageError) as exc_info:
        await validate_and_freeze_lineage(payload=_share_payload(), refs=[_share_ref()], owner=OWNER, loader=loader)
    assert exc_info.value.code == "derivation_tool_call_invalid"


async def test_derivation_nonexistent_tool_call_rejected() -> None:
    loader = MemoryLoader()
    loader.add_evidence("ev-1", "s-1", EVIDENCE_PAYLOAD)
    with pytest.raises(LineageError) as exc_info:
        await validate_and_freeze_lineage(payload=_share_payload(), refs=[_share_ref()], owner=OWNER, loader=loader)
    assert exc_info.value.code == "derivation_tool_call_invalid"


async def test_derivation_mcp_service_tool_call_rejected() -> None:
    loader = MemoryLoader()
    loader.add_evidence("ev-1", "s-1", EVIDENCE_PAYLOAD)
    loader.add_tool_call("tc-1", "s-1", service="mcp", status="settled")
    with pytest.raises(LineageError) as exc_info:
        await validate_and_freeze_lineage(payload=_share_payload(), refs=[_share_ref()], owner=OWNER, loader=loader)
    assert exc_info.value.code == "derivation_tool_call_invalid"


async def test_derivation_tool_call_cross_session_rejected() -> None:
    loader = MemoryLoader()
    loader.add_evidence("ev-1", "s-1", EVIDENCE_PAYLOAD)
    loader.add_tool_call("tc-1", "s-2", service="internal", status="settled")
    with pytest.raises(LineageError) as exc_info:
        await validate_and_freeze_lineage(payload=_share_payload(), refs=[_share_ref()], owner=OWNER, loader=loader)
    assert exc_info.value.code == "derivation_tool_call_invalid"


async def test_derivation_input_path_not_resolving_rejected() -> None:
    loader = MemoryLoader()
    loader.add_evidence("ev-1", "s-1", EVIDENCE_PAYLOAD)
    loader.add_tool_call("tc-1", "s-1", service="internal", status="settled")
    refs = [_share_ref(input_paths=("/99/nope",))]
    with pytest.raises(LineageError) as exc_info:
        await validate_and_freeze_lineage(payload=_share_payload(), refs=refs, owner=OWNER, loader=loader)
    assert exc_info.value.code == "derivation_input_path_not_found"


# ---------------------------------------------------------------------------
# 5. 递归闭包：展开到 Evidence / 无基座 / 环
# ---------------------------------------------------------------------------


async def test_artifact_chain_recursively_expands_to_evidence() -> None:
    loader = MemoryLoader()
    loader.add_evidence("ev-1", "s-1", EVIDENCE_PAYLOAD)
    loader.add_artifact_version(
        "va-1",
        "s-1",
        {"data": {"overview": {"total_volume": 100000}}},
        [_ref("/data/overview/total_volume").model_dump()],
    )
    # 更深的链：va-2 引用 va-1。
    loader.add_artifact_version(
        "va-2",
        "s-1",
        {"data": {"field": 1}},
        [
            {
                "artifact_path": "/data/field",
                "sources": [
                    {"source_type": "artifact", "artifact_version_id": "va-1", "source_path": "/data/overview/total_volume"}
                ],
                "derivation": None,
            }
        ],
    )
    payload = {"schema_version": "insight_board_v1", "data": {"final": 1}}
    refs = [
        LineageRef(
            artifact_path="/data/final",
            sources=(ArtifactSource(artifact_version_id="va-2", source_path="/data/field"),),
        )
    ]
    frozen = await validate_and_freeze_lineage(payload=payload, refs=refs, owner=OWNER, loader=loader)
    assert frozen.refs[0].sources == (
        FrozenEvidenceSource(evidence_id="ev-1", source_path="/0/声量", payload_hash="ph-ev-1"),
    )


async def test_artifact_source_with_no_lineage_base_rejected() -> None:
    loader = MemoryLoader()
    # va-1 的 payload 有 /data/overview/total_volume，但完全没有 lineage 引用。
    loader.add_artifact_version("va-1", "s-1", {"data": {"overview": {"total_volume": 100000}}}, [])
    payload = {"schema_version": "insight_board_v1", "data": {"grand_total": 100000}}
    refs = [
        LineageRef(
            artifact_path="/data/grand_total",
            sources=(ArtifactSource(artifact_version_id="va-1", source_path="/data/overview/total_volume"),),
        )
    ]
    with pytest.raises(LineageError) as exc_info:
        await validate_and_freeze_lineage(payload=payload, refs=refs, owner=OWNER, loader=loader)
    assert exc_info.value.code == "artifact_no_lineage_base"


async def test_artifact_cycle_rejected() -> None:
    loader = MemoryLoader()
    loader.add_artifact_version(
        "vA",
        "s-1",
        {"data": {"a": 1}},
        [
            {
                "artifact_path": "/data/a",
                "sources": [{"source_type": "artifact", "artifact_version_id": "vB", "source_path": "/data/b"}],
                "derivation": None,
            }
        ],
    )
    loader.add_artifact_version(
        "vB",
        "s-1",
        {"data": {"b": 1}},
        [
            {
                "artifact_path": "/data/b",
                "sources": [{"source_type": "artifact", "artifact_version_id": "vA", "source_path": "/data/a"}],
                "derivation": None,
            }
        ],
    )
    payload = {"schema_version": "insight_board_v1", "data": {"final": 1}}
    refs = [
        LineageRef(
            artifact_path="/data/final",
            sources=(ArtifactSource(artifact_version_id="vA", source_path="/data/a"),),
        )
    ]
    with pytest.raises(LineageError) as exc_info:
        await validate_and_freeze_lineage(payload=payload, refs=refs, owner=OWNER, loader=loader)
    assert exc_info.value.code == "lineage_cycle"


# ---------------------------------------------------------------------------
# 6. 必选 numeric 覆盖
# ---------------------------------------------------------------------------


async def test_insight_board_all_numeric_fields_covered() -> None:
    loader = MemoryLoader()
    loader.add_evidence("ev-1", "s-1", EVIDENCE_PAYLOAD)
    frozen = await validate_and_freeze_lineage(
        payload=_insight_payload(),
        refs=[_ref(path) for path in INSIGHT_REQUIRED_PATHS],
        owner=OWNER,
        loader=loader,
    )
    assert {ref.artifact_path for ref in frozen.refs} == set(INSIGHT_REQUIRED_PATHS)


async def test_missing_required_numeric_lineage_rejected() -> None:
    loader = MemoryLoader()
    loader.add_evidence("ev-1", "s-1", EVIDENCE_PAYLOAD)
    # 少一个 metric value 的 lineage 条目。
    refs = [_ref(path) for path in INSIGHT_REQUIRED_PATHS if path != "/data/blocks/0/cards/0/value"]
    with pytest.raises(LineageError) as exc_info:
        await validate_and_freeze_lineage(payload=_insight_payload(), refs=refs, owner=OWNER, loader=loader)
    assert exc_info.value.code == "missing_lineage"
    assert "/data/blocks/0/cards/0/value" in exc_info.value.message


async def test_date_version_weight_layout_excluded() -> None:
    loader = MemoryLoader()
    loader.add_evidence("ev-1", "s-1", EVIDENCE_PAYLOAD)
    payload = {
        "schema_version": "insight_board_v1",
        "data": {
            "blocks": [
                {
                    "block_type": "timeline",
                    "title": "里程碑",
                    "items": [{"date": "2026-01-01", "title": "发布", "description": ""}],
                },
                {
                    "block_type": "metric_grid",
                    "title": "概览",
                    "cards": [{"key": "volume", "label": "声量", "value": 100}],
                },
            ],
            "version": "1.0",
            "weight": 10,
        },
    }
    # 只有 metric value 需要 lineage；date/version/weight 都不要求。
    frozen = await validate_and_freeze_lineage(
        payload=payload,
        refs=[_ref("/data/blocks/1/cards/0/value")],
        owner=OWNER,
        loader=loader,
    )
    assert len(frozen.refs) == 1


async def test_scoring_formula_weights_excluded() -> None:
    loader = MemoryLoader()
    loader.add_evidence("ev-1", "s-1", EVIDENCE_PAYLOAD)
    payload = {
        "schema_version": "kol_selection_v3",
        "data": {
            "scoring": {
                "version": "kol_score_v2",
                "method": "weighted_sum",
                "weights": {"engagement": 20, "followers": 10},
                "missing_value_policy": "missing_as_zero",
            },
            "summary": {"candidate_count": 50},
        },
    }
    # scoring 公式权重是配置常量，不要求 lineage；summary.candidate_count 是业务数。
    frozen = await validate_and_freeze_lineage(
        payload=payload,
        refs=[_ref("/data/summary/candidate_count")],
        owner=OWNER,
        loader=loader,
    )
    assert len(frozen.refs) == 1


async def test_duplicate_artifact_path_rejected() -> None:
    loader = MemoryLoader()
    loader.add_evidence("ev-1", "s-1", EVIDENCE_PAYLOAD)
    payload = {"schema_version": "insight_board_v1", "data": {"a": 1}}
    with pytest.raises(LineageError) as exc_info:
        await validate_and_freeze_lineage(payload=payload, refs=[_ref("/data/a"), _ref("/data/a")], owner=OWNER, loader=loader)
    assert exc_info.value.code == "duplicate_artifact_path"


# ---------------------------------------------------------------------------
# 7. 冻结快照
# ---------------------------------------------------------------------------


def test_frozen_lineage_is_immutable() -> None:
    assert FrozenLineage.model_config.get("frozen") is True
    frozen = FrozenLineage(refs=())
    with pytest.raises(ValidationError):
        frozen.refs = ()  # type: ignore[misc]


async def test_frozen_snapshot_is_self_contained_closure() -> None:
    loader = MemoryLoader()
    loader.add_evidence("ev-1", "s-1", EVIDENCE_PAYLOAD)
    loader.add_artifact_version(
        "va-1",
        "s-1",
        {"data": {"overview": {"total_volume": 100000}}},
        [_ref("/data/overview/total_volume").model_dump()],
    )
    payload = {"schema_version": "insight_board_v1", "data": {"grand_total": 100000}}
    refs = [
        LineageRef(
            artifact_path="/data/grand_total",
            sources=(ArtifactSource(artifact_version_id="va-1", source_path="/data/overview/total_volume"),),
        )
    ]
    frozen = await validate_and_freeze_lineage(payload=payload, refs=refs, owner=OWNER, loader=loader)
    # 完整闭包：冻结快照里只剩 evidence 叶子，不再有 artifact 引用。
    for ref in frozen.refs:
        for source in ref.sources:
            assert source.evidence_id == "ev-1"
            assert source.source_path == "/0/声量"
            assert source.payload_hash == "ph-ev-1"


# ---------------------------------------------------------------------------
# 8. DB loader（MySQL 端到端）
# ---------------------------------------------------------------------------


async def _make_db_chain(db_session, user_id: str) -> tuple[AgentSession, AgentRun, AgentStep]:
    now = datetime.now(UTC).replace(tzinfo=None)
    session = AgentSession(
        id=str(uuid4()), user_id=user_id, title="会话", status="active", created_at=now, updated_at=now
    )
    db_session.add(session)
    await db_session.flush()
    run = AgentRun(
        id=str(uuid4()),
        session_id=session.id,
        user_id=user_id,
        profile_name="session_analyst_v1",
        profile_version="v1",
        model="test-model",
        status="running",
    )
    db_session.add(run)
    await db_session.flush()
    attempt = AgentRunAttempt(id=str(uuid4()), run_id=run.id, attempt=1, started_at=now)
    db_session.add(attempt)
    await db_session.flush()
    step = AgentStep(
        id=str(uuid4()),
        run_id=run.id,
        attempt_id=attempt.id,
        sequence=1,
        step_type="tool_call",
        status="running",
        created_at=now,
    )
    db_session.add(step)
    await db_session.flush()
    return session, run, step


async def test_db_loader_resolves_evidence_and_tool_call(db_session, user_factory) -> None:
    user = await user_factory()
    session, run, step = await _make_db_chain(db_session, user.id)
    tool = AgentToolCall(
        id=str(uuid4()),
        run_id=run.id,
        step_id=step.id,
        logical_call_id="logical-1",
        service="internal",
        internal_tool_name="divide",
        arguments_json={},
        arguments_hash="args-hash",
        status="settled",
        points_reserved=0,
        points_settled=0,
        started_at=datetime.now(UTC).replace(tzinfo=None),
        completed_at=datetime.now(UTC).replace(tzinfo=None),
    )
    db_session.add(tool)
    await db_session.flush()
    evidence = EvidenceItem(
        id=str(uuid4()),
        session_id=session.id,
        run_id=run.id,
        tool_call_id=tool.id,
        source_type="mcp",
        source_name="声量来源",
        raw_payload_json=EVIDENCE_PAYLOAD,
        payload_hash="evidence-hash",
        collected_at=datetime.now(UTC).replace(tzinfo=None),
        availability_status="available",
    )
    db_session.add(evidence)
    await db_session.flush()

    loader = DbLineageLoader(db_session)
    refs = [
        LineageRef(
            artifact_path="/data/positive_share",
            sources=(
                EvidenceSource(evidence_id=evidence.id, source_path="/0/正面声量数"),
                EvidenceSource(evidence_id=evidence.id, source_path="/0/声量"),
            ),
            derivation=DerivationRef(tool_call_id=tool.id, method="divide", input_paths=("/0/正面声量数", "/0/声量")),
        )
    ]
    frozen = await validate_and_freeze_lineage(
        payload=_share_payload(),
        refs=refs,
        owner=LineageOwner(user_id=user.id, session_id=session.id),
        loader=loader,
    )
    assert frozen.refs[0].sources[0].evidence_id == evidence.id
    assert frozen.refs[0].sources[0].payload_hash == "evidence-hash"
    assert frozen.refs[0].derivation is not None
