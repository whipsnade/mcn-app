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
    MAX_ARTIFACT_DEPTH,
    ArtifactLineageFreezer,
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
    FrozenDerivationRef,
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
        # 记录每个 artifact version 的查询次数，用于断言菱形展开去重。
        self.artifact_loads: dict[str, int] = {}

    def add_evidence(
        self,
        evidence_id: str,
        session_id: str,
        payload: object,
        *,
        run_id: str | None = None,
        upload: dict | None = None,
    ) -> None:
        self.evidence[evidence_id] = {
            "session_id": session_id,
            "payload": payload,
            "payload_hash": f"ph-{evidence_id}",
            "run_id": run_id,
            "upload": upload,
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
            run_id=row["run_id"],
            upload=row["upload"],
        )

    async def load_artifact_version(self, version_id: str) -> ArtifactVersionRecord | None:
        row = self.artifact_versions.get(version_id)
        if row is None:
            return None
        self.artifact_loads[version_id] = self.artifact_loads.get(version_id, 0) + 1
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


async def test_direct_mcp_evidence_from_other_run_is_rejected() -> None:
    loader = MemoryLoader()
    loader.add_evidence("ev-old", "s-1", EVIDENCE_PAYLOAD, run_id="run-old")
    payload = {"schema_version": "insight_board_v1", "data": {"total_volume": 100}}
    refs = [
        LineageRef(
            artifact_path="/data/total_volume",
            sources=(EvidenceSource(evidence_id="ev-old", source_path="/0/声量"),),
        )
    ]
    with pytest.raises(LineageError) as exc_info:
        await validate_and_freeze_lineage(
            payload=payload,
            refs=refs,
            owner=LineageOwner(user_id="u-1", session_id="s-1", run_id="run-current"),
            loader=loader,
        )
    assert exc_info.value.code == "evidence_run_not_owned"


async def test_same_session_historical_artifact_allows_old_run_evidence() -> None:
    loader = MemoryLoader()
    loader.add_evidence("ev-old", "s-1", EVIDENCE_PAYLOAD, run_id="run-old")
    loader.add_artifact_version(
        "va-old",
        "s-1",
        {"data": {"overview": {"total_volume": 100}}},
        [
            {
                "artifact_path": "/data/overview/total_volume",
                "sources": [
                    {
                        "source_type": "evidence",
                        "evidence_id": "ev-old",
                        "source_path": "/0/声量",
                    }
                ],
            }
        ],
    )
    payload = {"schema_version": "insight_board_v1", "data": {"total_volume": 100}}
    frozen = await validate_and_freeze_lineage(
        payload=payload,
        refs=[
            LineageRef(
                artifact_path="/data/total_volume",
                sources=(
                    ArtifactSource(
                        artifact_version_id="va-old",
                        source_path="/data/overview/total_volume",
                    ),
                ),
            )
        ],
        owner=LineageOwner(user_id="u-1", session_id="s-1", run_id="run-current"),
        loader=loader,
    )
    assert frozen.refs[0].sources == (
        FrozenEvidenceSource(
            evidence_id="ev-old", source_path="/0/声量", payload_hash="ph-ev-old"
        ),
    )
    assert loader.tool_calls == {}


async def test_historical_artifact_preserves_uploaded_evidence_snapshot() -> None:
    loader = MemoryLoader()
    loader.add_evidence(
        "ev-upload-old",
        "s-1",
        [{"声量": 100}],
        run_id="run-old",
        upload={
            "upload_id": "upload-old",
            "sha256": "u" * 64,
            "original_filename": "old.csv",
            "uploaded_at": "2026-08-01T00:00:00",
        },
    )
    loader.add_artifact_version(
        "va-upload-old",
        "s-1",
        {"data": {"total_volume": 100}},
        [
            {
                "artifact_path": "/data/total_volume",
                "sources": [
                    {
                        "source_type": "evidence",
                        "evidence_id": "ev-upload-old",
                        "source_path": "/0/声量",
                    }
                ],
            }
        ],
    )
    frozen = await validate_and_freeze_lineage(
        payload={"schema_version": "insight_board_v1", "data": {"total_volume": 100}},
        refs=[
            {
                "artifact_path": "/data/total_volume",
                "sources": [
                    {
                        "source_type": "artifact",
                        "artifact_version_id": "va-upload-old",
                        "source_path": "/data/total_volume",
                    }
                ],
            }
        ],
        owner=LineageOwner(user_id="u-1", session_id="s-1", run_id="run-current"),
        loader=loader,
    )
    assert frozen.refs[0].sources == (
        FrozenEvidenceSource(
            evidence_id="ev-upload-old",
            source_path="/0/声量",
            payload_hash="ph-ev-upload-old",
            upload_id="upload-old",
            upload_sha256="u" * 64,
            upload_filename="old.csv",
            uploaded_at="2026-08-01T00:00:00",
        ),
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


async def test_date_and_version_layout_excluded() -> None:
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
        },
    }
    # 只有 metric value 需要 lineage；date 组合与 version 都是例外。
    frozen = await validate_and_freeze_lineage(
        payload=payload,
        refs=[_ref("/data/blocks/1/cards/0/value")],
        owner=OWNER,
        loader=loader,
    )
    assert len(frozen.refs) == 1


async def test_insight_metric_named_weight_requires_lineage() -> None:
    """Fix 3：``weight`` 不是全局例外键——业务 metric 名为 weight 也必须可溯源。"""
    loader = MemoryLoader()
    loader.add_evidence("ev-1", "s-1", EVIDENCE_PAYLOAD)
    payload = {
        "schema_version": "insight_board_v1",
        "data": {
            "blocks": [
                {
                    "block_type": "metric_grid",
                    "title": "物流",
                    "cards": [{"key": "weight", "label": "物流权重", "value": 3.5}],
                }
            ]
        },
    }
    with pytest.raises(LineageError) as exc_info:
        await validate_and_freeze_lineage(payload=payload, refs=[], owner=OWNER, loader=loader)
    assert exc_info.value.code == "missing_lineage"
    assert "/data/blocks/0/cards/0/value" in exc_info.value.message
    # 补齐 lineage 后通过。
    frozen = await validate_and_freeze_lineage(
        payload=payload,
        refs=[_ref("/data/blocks/0/cards/0/value")],
        owner=OWNER,
        loader=loader,
    )
    assert len(frozen.refs) == 1


async def test_scoring_formula_weights_excluded() -> None:
    """Fix 3：评分公式常量权重（scoring.weights.* 与 score_snapshot.dimensions.*.weight）
    是配置常量，不要求 lineage；其余业务数（rank/total/raw_score/weighted_score/
    candidate_count）都要求。"""
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
            "items": [
                {
                    "rank": 1,
                    "platform": "xiaohongshu",
                    "kol_uid": "k1",
                    "score_snapshot": {
                        "version": "kol_score_v2",
                        "total": 78.0,
                        "rating": "重点推荐",
                        "stars": "★★★★★",
                        "data_completeness": 100.0,
                        "dimensions": {
                            "engagement": {"raw_score": 80.0, "weight": 20, "weighted_score": 16.0}
                        },
                    },
                }
            ],
            "summary": {"candidate_count": 50},
        },
    }
    required_paths = (
        "/data/items/0/rank",
        "/data/items/0/score_snapshot/total",
        "/data/items/0/score_snapshot/data_completeness",
        "/data/items/0/score_snapshot/dimensions/engagement/raw_score",
        "/data/items/0/score_snapshot/dimensions/engagement/weighted_score",
        "/data/summary/candidate_count",
    )
    frozen = await validate_and_freeze_lineage(
        payload=payload,
        refs=[_ref(path) for path in required_paths],
        owner=OWNER,
        loader=loader,
    )
    assert {ref.artifact_path for ref in frozen.refs} == set(required_paths)


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


async def test_frozen_snapshot_does_not_alias_input_derivation() -> None:
    """Fix 1：冻结后修改可变输入 ref 的 derivation 不得污染已发布快照。"""
    loader = MemoryLoader()
    loader.add_evidence("ev-1", "s-1", EVIDENCE_PAYLOAD)
    loader.add_tool_call("tc-1", "s-1", service="internal", status="settled")
    source_ref = _share_ref()
    frozen = await validate_and_freeze_lineage(
        payload=_share_payload(), refs=[source_ref], owner=OWNER, loader=loader
    )
    assert frozen.refs[0].derivation is not None
    assert isinstance(frozen.refs[0].derivation, FrozenDerivationRef)
    assert frozen.refs[0].derivation.method == "divide"
    # 快照持有独立副本，且 FrozenDerivationRef 自身不可变。
    source_ref.derivation.method = "multiply"  # type: ignore[union-attr]
    assert frozen.refs[0].derivation.method == "divide"
    with pytest.raises(ValidationError):
        frozen.refs[0].derivation.method = "add"  # type: ignore[index]


async def test_diamond_chain_loads_each_version_once() -> None:
    """Fix 2：菱形共享子图（V→A/B→C）每个 artifact version 只查询一次。"""
    loader = MemoryLoader()
    loader.add_evidence("ev-1", "s-1", EVIDENCE_PAYLOAD)

    def ref_to(next_version_id: str | None) -> dict:
        """每个版本引用同一字段 /data/x：叶子指向 evidence，否则指向下一版本。"""
        sources = (
            [{"source_type": "evidence", "evidence_id": "ev-1", "source_path": "/0/声量"}]
            if next_version_id is None
            else [
                {
                    "source_type": "artifact",
                    "artifact_version_id": next_version_id,
                    "source_path": "/data/x",
                }
            ]
        )
        return {"artifact_path": "/data/x", "sources": sources, "derivation": None}

    loader.add_artifact_version("vC", "s-1", {"data": {"x": 1}}, [ref_to(None)])
    loader.add_artifact_version("vA", "s-1", {"data": {"x": 1}}, [ref_to("vC")])
    loader.add_artifact_version("vB", "s-1", {"data": {"x": 1}}, [ref_to("vC")])
    loader.add_artifact_version(
        "vV",
        "s-1",
        {"data": {"x": 1}},
        [
            {
                "artifact_path": "/data/x",
                "sources": [
                    {"source_type": "artifact", "artifact_version_id": "vA", "source_path": "/data/x"},
                    {"source_type": "artifact", "artifact_version_id": "vB", "source_path": "/data/x"},
                ],
                "derivation": None,
            }
        ],
    )
    payload = {"schema_version": "insight_board_v1", "data": {"final": 1}}
    refs = [
        LineageRef(
            artifact_path="/data/final",
            sources=(ArtifactSource(artifact_version_id="vV", source_path="/data/x"),),
        )
    ]
    frozen = await validate_and_freeze_lineage(payload=payload, refs=refs, owner=OWNER, loader=loader)
    # 证据叶子去重为一条，且每个版本仅查询一次。
    assert len(frozen.refs[0].sources) == 1
    for version_id in ("vV", "vA", "vB", "vC"):
        assert loader.artifact_loads.get(version_id) == 1, version_id


async def test_excessively_deep_chain_rejected() -> None:
    """Fix 2：超过 MAX_ARTIFACT_DEPTH 的递归链拒绝（lineage_too_deep）。"""
    loader = MemoryLoader()
    loader.add_evidence("ev-1", "s-1", EVIDENCE_PAYLOAD)
    leaf_version = f"v{MAX_ARTIFACT_DEPTH}"
    loader.add_artifact_version(
        leaf_version,
        "s-1",
        {"data": {"f": 1}},
        [
            {
                "artifact_path": "/data/f",
                "sources": [
                    {"source_type": "evidence", "evidence_id": "ev-1", "source_path": "/0/声量"}
                ],
                "derivation": None,
            }
        ],
    )
    for index in range(MAX_ARTIFACT_DEPTH - 1, -1, -1):
        loader.add_artifact_version(
            f"v{index}",
            "s-1",
            {"data": {"f": 1}},
            [
                {
                    "artifact_path": "/data/f",
                    "sources": [
                        {
                            "source_type": "artifact",
                            "artifact_version_id": f"v{index + 1}",
                            "source_path": "/data/f",
                        }
                    ],
                    "derivation": None,
                }
            ],
        )
    payload = {"schema_version": "insight_board_v1", "data": {"final": 1}}
    refs = [
        LineageRef(
            artifact_path="/data/final",
            sources=(ArtifactSource(artifact_version_id="v0", source_path="/data/f"),),
        )
    ]
    with pytest.raises(LineageError) as exc_info:
        await validate_and_freeze_lineage(payload=payload, refs=refs, owner=OWNER, loader=loader)
    assert exc_info.value.code == "lineage_too_deep"


async def test_array_index_leading_zeros_rejected() -> None:
    """Minor：RFC 6901 数组下标不允许前导零（"/00/..." 非法）。"""
    loader = MemoryLoader()
    loader.add_evidence("ev-1", "s-1", EVIDENCE_PAYLOAD)
    payload = {"schema_version": "insight_board_v1", "data": {"overview": {"total_volume": 100000}}}
    refs = [
        LineageRef(
            artifact_path="/data/overview/total_volume",
            sources=(EvidenceSource(evidence_id="ev-1", source_path="/00/声量"),),
        )
    ]
    with pytest.raises(LineageError) as exc_info:
        await validate_and_freeze_lineage(payload=payload, refs=refs, owner=OWNER, loader=loader)
    assert exc_info.value.code == "evidence_source_path_not_found"


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


async def test_publish_freezes_upload_hash(db_session, user_factory) -> None:
    """upload Evidence 的冻结快照携带上传文件哈希（Gate B：可追溯到源文件）。

    MCP Evidence 的 tool_call_id 在快照中保留；upload Evidence 的
    tool_call_id 为 NULL，upload_id/sha256/文件名/上传时间随冻结闭包固化。
    """
    from app.agent_runtime.models import AgentUpload

    user = await user_factory()
    session, _run, _step = await _make_db_chain(db_session, user.id)
    now = datetime.now(UTC).replace(tzinfo=None)
    upload = AgentUpload(
        id=str(uuid4()),
        user_id=user.id,
        session_id=session.id,
        original_filename="投放数据.csv",
        mime_type="text/csv",
        size_bytes=120,
        sha256="u" * 64,
        storage_key=f"{user.id}/abcd1234-{('u' * 16)}.csv",
        status="parsed",
        created_at=now,
        completed_at=now,
    )
    db_session.add(upload)
    await db_session.flush()
    evidence = EvidenceItem(
        id=str(uuid4()),
        session_id=session.id,
        run_id=None,
        tool_call_id=None,
        upload_id=upload.id,
        source_type="user_upload",
        source_name="user_upload",
        raw_payload_json={"columns": ["平台", "声量"], "rows": [{"平台": "小红书", "声量": 100}]},
        payload_hash="upload-evidence-hash",
        collected_at=now,
        availability_status="available",
    )
    db_session.add(evidence)
    await db_session.flush()

    freezer = ArtifactLineageFreezer(db_session)
    snapshot = await freezer.freeze(
        payload={"schema_version": "insight_board_v1", "data": {"total_volume": 100}},
        refs=[
            {
                "artifact_path": "/data/total_volume",
                "sources": [
                    {
                        "source_type": "evidence",
                        "evidence_id": evidence.id,
                        "source_path": "/rows/0/声量",
                    }
                ],
            }
        ],
        owner=LineageOwner(user_id=user.id, session_id=session.id),
    )
    source = snapshot["refs"][0]["sources"][0]
    assert source["evidence_id"] == evidence.id
    assert source["upload_sha256"] == upload.sha256
    assert source["upload_id"] == upload.id
    assert source["upload_filename"] == "投放数据.csv"
    assert source["tool_call_id"] is None
