"""kol_analysis_v2 Draft builder + Artifact Draft 工具测试（设计 §12.1/§12.3）。

Builder 覆盖：
1. parent 版本绑定：kol_analysis_v2 必须固定 parent_artifact_version_id 到分析的
   名单版本；scope.selection_artifact_id/selection_version 正确；
2. 稳定身份复用：同一 selection 可复用 kol-analysis:{selection_artifact_id} 稳定
   身份，旧版本 parent 绑定不变（不可变）；
3. 五个分布 + kol_trend/top_kols（≤20）+ narrative supporting_paths；
4. lineage 经 Task 11 递归到名单 Evidence。

Draft 工具覆盖（经 ToolRegistry，Task 7）：
- create_draft / update_draft 注册为 ARTIFACT_TOOLS、零积分、external_side_effect；
- H2/H5 强类型护栏：六类强类型（含 insight_board_v1）直写一律
  typed_artifact_requires_builder 并回指对应 Builder。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.agent_artifacts.builders.common import DraftBuildError
from app.agent_artifacts.builders.kol_analysis import build_kol_analysis_draft
from app.agent_artifacts.lineage import (
    ArtifactVersionRecord,
    EvidenceRecord,
    LineageOwner,
    ToolCallRecord,
    validate_and_freeze_lineage,
)
from app.agent_artifacts.models import (
    AgentArtifact,
    AgentArtifactVersion,
    ArtifactDraftRevision,
)
from app.agent_artifacts.payloads.kol_analysis import KolAnalysisV2
from app.agent_artifacts.service import ArtifactService
from app.agent_runtime.profiles import ARTIFACT_TOOLS, PROFILES
from app.agent_runtime.tools.artifacts import CreateDraftArgs, CreateDraftTool, UpdateDraftTool
from app.agent_runtime.tools.builders import (
    BuildKolAnalysisDraftArgs,
    BuildKolAnalysisDraftTool,
)
from app.agent_runtime.tools.contracts import ToolContext
from app.agent_runtime.tools.registry import ToolRegistry

from tests.agent_artifacts.payload_fixtures import insight_payload

session_analyst = PROFILES["session_analyst_v1"]

WEIGHTS = {
    "industry_interest": 10,
    "target_region": 8,
    "target_age": 8,
    "engagement": 20,
    "active_follower": 15,
    "content": 15,
    "followers": 10,
    "engagement_follower_ratio": 14,
}

ANALYSIS_CTX = ToolContext(
    user_id="u-1",
    session_id="s-1",
    run_id="r-1",
    profile_name="session_analyst_v1",
)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _selection_item(
    uid: str,
    platform: str = "小红书",
    followers: int = 500_000,
    engagement_total: int = 100,
    score: float = 70.0,
    rating: str = "推荐",
    regions: tuple[str, ...] = ("上海",),
    rank: int = 1,
) -> dict[str, Any]:
    return {
        "rank": rank,
        "platform": platform,
        "kol_uid": uid,
        "nickname": f"达人{uid}",
        "followers": followers,
        "active_followers": 300_000,
        "active_follower_rate": 0.6,
        "growth_rate": 0.3,
        "engagement_total": engagement_total,
        "avg_engagement": 1.0,
        "likes": 50,
        "comments": 30,
        "shares": 20,
        "quoted_price": 800,
        "reasons": [],
        "missing_fields": [],
        "audience": {"regions": list(regions), "age_ranges": ["18-24"], "interests": ["美食"]},
        "score_snapshot": {
            "version": "kol_score_v2",
            "total": score,
            "rating": rating,
            "stars": "★★★★",
            "data_completeness": 100.0,
            "dimensions": {
                dim: {
                    "raw_score": 50.0,
                    "weight": weight,
                    "weighted_score": 0.5,
                    "source": None,
                    "missing_reason": None,
                }
                for dim, weight in WEIGHTS.items()
            },
        },
    }


def _selection_payload(items: list[dict[str, Any]]) -> dict[str, Any]:
    """完整合法 kol_selection_v3（A5 起 Draft 必须过强类型校验）。

    分析 builder 只读 ``data.items``；标准化 dump 保证「输入即存储形态」。
    超过 Top20 上限时返回未校验的历史版本形态（分析 builder 的截断测试需要
    25 条候选；发布侧的 Top20 约束由 KolSelectionV3 保证）。
    """
    from app.agent_artifacts.payloads.kol_selection import KolSelectionV3

    payload = {
        "schema_version": "kol_selection_v3",
        "module": "kol",
        "data_status": "complete",
        "availability": {
            section: {"status": "complete", "reason_codes": []}
            for section in ("scoring", "items", "summary")
        },
        "limitations": [],
        "methodology": {
            "data_as_of": datetime(2026, 1, 15, 12, 0),
            "source_names": ["DataTap"],
            "notes": [],
        },
        "scope": {
            "brand": "某品牌",
            "category": None,
            "campaign": None,
            "platforms": ["小红书"],
            "audience": {"regions": [], "age_ranges": [], "interests": []},
            "filters": {
                "budget_min": None,
                "budget_max": None,
                "follower_min": None,
                "follower_max": None,
            },
        },
        "data": {
            "scoring": {
                "version": "kol_score_v2",
                "method": "weighted_sum",
                "weights": WEIGHTS,
                "missing_value_policy": "missing_as_zero",
            },
            "items": items,
            "summary": {
                "candidate_count": len(items),
                "selected_count": len(items),
                "platform_distribution": [],
                "rating_distribution": [],
            },
        },
        "narrative": {
            "selection_summary": "名单",
            "fit_findings": [],
            "risk_notes": [],
            "usage_advice": [],
        },
    }
    if len(items) > 20:
        return payload
    return KolSelectionV3.model_validate(payload).model_dump(mode="json")


def _selection_refs(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """名单版本的代表性 lineage：覆盖分析引用所需的各数字路径。

    只用于让分析 builder 能构建 lineage（非 lineage 用例）；断言级 lineage 覆盖
    由 ``test_analysis_lineage_recurses_to_selection_evidence`` 负责。
    """
    refs: list[dict[str, Any]] = []
    for i in range(len(items)):
        refs.append(
            {
                "artifact_path": f"/data/items/{i}/rank",
                "sources": [
                    {"source_type": "evidence", "evidence_id": "ev-1", "source_path": f"/{i}/kol_uid"}
                ],
                "derivation": None,
            }
        )
        for field in ("followers", "active_followers", "engagement_total", "avg_engagement", "growth_rate"):
            refs.append(
                {
                    "artifact_path": f"/data/items/{i}/{field}",
                    "sources": [
                        {"source_type": "evidence", "evidence_id": "ev-1", "source_path": f"/{i}/{field}"}
                    ],
                    "derivation": None,
                }
            )
        refs.append(
            {
                "artifact_path": f"/data/items/{i}/score_snapshot/total",
                "sources": [
                    {
                        "source_type": "evidence",
                        "evidence_id": "ev-1",
                        "source_path": f"/{i}/engagement_total",
                    }
                ],
                "derivation": None,
            }
        )
    refs.append(
        {
            "artifact_path": "/data/summary/candidate_count",
            "sources": [
                {"source_type": "evidence", "evidence_id": "ev-1", "source_path": "/0/kol_uid"}
            ],
            "derivation": None,
        }
    )
    return refs


class MemoryLoader:
    """测试用内存 lineage loader：注入 Evidence / Artifact Version。"""

    def __init__(self) -> None:
        self.evidence: dict[str, dict] = {}
        self.artifact_versions: dict[str, dict] = {}
        self.tool_calls: dict[str, dict] = {}

    def add_evidence(self, evidence_id: str, session_id: str, payload: Any) -> None:
        self.evidence[evidence_id] = {"session_id": session_id, "payload": payload}

    def add_artifact_version(
        self, version_id: str, session_id: str, payload: Any, evidence_refs: list[dict]
    ) -> None:
        self.artifact_versions[version_id] = {
            "session_id": session_id,
            "payload": payload,
            "evidence_refs": evidence_refs,
        }

    async def load_evidence(self, evidence_id: str) -> EvidenceRecord | None:
        row = self.evidence.get(evidence_id)
        if row is None:
            return None
        return EvidenceRecord(
            id=evidence_id,
            session_id=row["session_id"],
            raw_payload=row["payload"],
            payload_hash=f"ph-{evidence_id}",
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


# ---------------------------------------------------------------------------
# 1. Parent 版本绑定
# ---------------------------------------------------------------------------


def test_parent_version_binding_fixed() -> None:
    items = [_selection_item("1")]
    selection_payload = _selection_payload(items)
    build = build_kol_analysis_draft(
        selection_artifact_id="artifact-A",
        selection_payload=selection_payload,
        parent_artifact_version_id="version-V1",
        selection_version="1",
        selection_refs=_selection_refs(items),
    )
    # 每个 kol_analysis_v2 通过 parent_artifact_version_id 固定到分析的名单版本。
    assert build.parent_artifact_version_id == "version-V1"
    assert build.payload["scope"]["selection_artifact_id"] == "artifact-A"
    assert build.payload["scope"]["selection_version"] == "1"
    assert build.payload["scope"]["analysis_period"] is None
    # Draft 工具据此生成稳定身份 kol-analysis:{selection_artifact_id}。
    assert build.module == "kol-analysis"
    assert build.business_fields == {"selection_artifact_id": "artifact-A"}
    KolAnalysisV2.model_validate(build.payload)


def test_analysis_builder_requires_selection_refs() -> None:
    """缺少名单版本 evidence_refs 时 fail-fast，而不是静默丢弃 lineage。"""
    items = [_selection_item("1")]
    with pytest.raises(DraftBuildError):
        build_kol_analysis_draft(
            selection_artifact_id="A",
            selection_payload=_selection_payload(items),
            parent_artifact_version_id="V1",
            selection_version="1",
        )


def test_analysis_builder_wraps_payload_validation_error() -> None:
    """Schema 校验失败统一抛 DraftBuildError，而不是泄漏裸 ValidationError。"""
    items = [_selection_item("1")]
    with patch(
        "app.agent_artifacts.builders.kol_analysis.KolAnalysisV2.model_validate",
        side_effect=ValidationError.from_exception_data("kol_analysis_v2", []),
    ):
        with pytest.raises(DraftBuildError):
            build_kol_analysis_draft(
                selection_artifact_id="A",
                selection_payload=_selection_payload(items),
                parent_artifact_version_id="V1",
                selection_version="1",
                selection_refs=_selection_refs(items),
            )


# ---------------------------------------------------------------------------
# 3. 分布 + kol_trend/top_kols + narrative
# ---------------------------------------------------------------------------


def test_distributions_and_narrative_build_correctly() -> None:
    items = [
        _selection_item("1", platform="小红书", followers=200_000, engagement_total=30_000, score=80.0, rating="重点推荐", regions=("上海",)),
        _selection_item("2", platform="抖音", followers=800_000, engagement_total=1_000, score=50.0, rating="观察", regions=("上海", "北京")),
        _selection_item("3", platform="小红书", followers=2_000_000, engagement_total=120_000, score=65.0, rating="推荐", regions=("广州",)),
    ]
    build = build_kol_analysis_draft(
        selection_artifact_id="A",
        selection_payload=_selection_payload(items),
        parent_artifact_version_id="V1",
        selection_version="1",
        selection_refs=_selection_refs(items),
    )
    data = build.payload["data"]
    assert data["summary"]["kol_count"] == 3
    assert data["summary"]["total_followers"] == 200_000 + 800_000 + 2_000_000
    assert data["summary"]["total_engagement"] == 30_000 + 1_000 + 120_000
    assert data["summary"]["avg_score"] == pytest.approx(round((80 + 50 + 65) / 3, 2))

    distribution_names = (
        "platform_distribution",
        "rating_distribution",
        "follower_distribution",
        "engagement_distribution",
        "region_distribution",
    )
    for name in distribution_names:
        for entry in data[name]:
            assert set(entry) == {"key", "label", "count", "share"}

    by_platform = {entry["key"]: entry for entry in data["platform_distribution"]}
    assert by_platform["小红书"]["count"] == 2
    assert by_platform["抖音"]["count"] == 1
    assert by_platform["小红书"]["share"] == pytest.approx(round(2 / 3, 4))

    assert len(data["kol_trend"]) == 3
    assert len(data["top_kols"]) == 3
    assert data["top_kols"][0]["rank"] == 1
    assert data["top_kols"][0]["kol_uid"] == "1"

    # narrative 通过 supporting_paths 引用 data。
    narrative = build.payload["narrative"]
    assert narrative["executive_summary"]
    for section in ("portfolio_findings", "mix_recommendations", "risk_notes"):
        for finding in narrative[section]:
            assert finding["supporting_paths"]

    KolAnalysisV2.model_validate(build.payload)


def test_top_kols_and_kol_trend_capped_at_20() -> None:
    items = [_selection_item(str(i), followers=100_000 + i, engagement_total=100 + i) for i in range(25)]
    build = build_kol_analysis_draft(
        selection_artifact_id="A",
        selection_payload=_selection_payload(items),
        parent_artifact_version_id="V1",
        selection_version="1",
        selection_refs=_selection_refs(items),
    )
    assert len(build.payload["data"]["kol_trend"]) == 20
    assert len(build.payload["data"]["top_kols"]) == 20
    assert build.payload["data"]["summary"]["kol_count"] == 25


# ---------------------------------------------------------------------------
# 3.1 模型叙事（H4，设计 §6.1）：Reviewer 要求逐人分析，确定性组合级模板
# 叙事无法满足；模型叙事经 KolAnalysisV2 强校验后写入 payload。
# ---------------------------------------------------------------------------


def test_analysis_builder_model_narrative_passthrough() -> None:
    """模型叙事替代确定性兜底，写入 payload 且过强校验。"""
    items = [_selection_item("1")]
    narrative = {
        "executive_summary": "名单 1 位达人，价值集中。",
        "portfolio_findings": [
            {
                "title": "达人1 核心价值",
                "detail": "评分与互动量头部。",
                "supporting_paths": ["data.top_kols.0.score"],
            }
        ],
        "mix_recommendations": [],
        "risk_notes": [],
    }
    build = build_kol_analysis_draft(
        selection_artifact_id="A",
        selection_payload=_selection_payload(items),
        parent_artifact_version_id="V1",
        selection_version="1",
        selection_refs=_selection_refs(items),
        narrative=narrative,
    )
    assert build.payload["narrative"]["executive_summary"] == "名单 1 位达人，价值集中。"
    assert build.payload["narrative"]["portfolio_findings"][0]["title"] == "达人1 核心价值"
    KolAnalysisV2.model_validate(build.payload)


def test_analysis_builder_model_narrative_invalid_supporting_path_raises() -> None:
    """模型叙事的 supporting_paths 指向 data 内不存在的路径 → DraftBuildError
    （工具层转 draft_build_error 结构化回喂）。"""
    items = [_selection_item("1")]
    narrative = {
        "executive_summary": "概览。",
        "portfolio_findings": [
            {"title": "幻觉", "detail": "引用不存在。", "supporting_paths": ["data.top_kols.99.score"]}
        ],
        "mix_recommendations": [],
        "risk_notes": [],
    }
    with pytest.raises(DraftBuildError):
        build_kol_analysis_draft(
            selection_artifact_id="A",
            selection_payload=_selection_payload(items),
            parent_artifact_version_id="V1",
            selection_version="1",
            selection_refs=_selection_refs(items),
            narrative=narrative,
        )


def test_analysis_builder_model_narrative_ignored_when_selection_empty() -> None:
    """无候选的 restricted 路径恒用 builder 受限披露叙事（此时无 data 可引用，
    不采用模型叙事）。"""
    build = build_kol_analysis_draft(
        selection_artifact_id="A",
        selection_payload=_selection_payload([]),
        parent_artifact_version_id="V1",
        selection_version="1",
        narrative={
            "executive_summary": "模型叙事不应生效。",
            "portfolio_findings": [],
            "mix_recommendations": [],
            "risk_notes": [],
        },
    )
    assert build.payload["data_status"] == "restricted"
    assert build.payload["narrative"]["executive_summary"] == "名单数据不足，无法完成 KOL 分析。"
    KolAnalysisV2.model_validate(build.payload)


# ---------------------------------------------------------------------------
# 4. Lineage 递归到名单 Evidence
# ---------------------------------------------------------------------------


async def test_analysis_lineage_recurses_to_selection_evidence() -> None:
    items = [
        _selection_item("1", engagement_total=100),
        _selection_item("2", engagement_total=50),
    ]

    evidence = items
    selection_refs = _selection_refs(items)
    selection_payload = _selection_payload(items)

    build = build_kol_analysis_draft(
        selection_artifact_id="A",
        selection_payload=selection_payload,
        parent_artifact_version_id="sel-version-1",
        selection_version="1",
        selection_refs=selection_refs,
    )
    KolAnalysisV2.model_validate(build.payload)

    loader = MemoryLoader()
    loader.add_evidence("ev-1", "s-1", evidence)
    loader.add_artifact_version("sel-version-1", "s-1", selection_payload, selection_refs)

    frozen = await validate_and_freeze_lineage(
        payload=build.payload,
        refs=build.evidence_refs,
        owner=LineageOwner(user_id="u-1", session_id="s-1"),
        loader=loader,
    )
    assert frozen.refs
    # 全部来源经递归展开为 evidence 叶子。
    for ref in frozen.refs:
        assert ref.sources
        for source in ref.sources:
            assert source.evidence_id == "ev-1"


# ---------------------------------------------------------------------------
# 2. 稳定身份复用 + 旧版本 parent 绑定不可变（经 build_kol_analysis_draft 工具）
# ---------------------------------------------------------------------------


async def test_stable_identity_reuse_keeps_old_parent_binding(
    db_session, user_factory, session_factory, run_factory,
) -> None:
    user = await user_factory()
    session = await session_factory(user.id)
    run = await run_factory(session.id, user.id)
    ctx = ToolContext(
        user_id=user.id,
        session_id=session.id,
        run_id=run.id,
        profile_name="session_analyst_v1",
    )
    service = ArtifactService(db_session)
    # H2：kol_analysis_v2 直写 create_draft 已被 typed_artifact_requires_builder
    # 护栏拒绝，parent 绑定链路改经 Builder 工具验证（同一 create_or_get 语义）。
    tool = BuildKolAnalysisDraftTool(db_session)

    # 先建名单 Artifact + 已发布 version V1（作为分析的不可变父版本）。
    selection_payload_v1 = _selection_payload([_selection_item("1")])
    sel_artifact, sel_draft, sel_rev = await service.create_or_get_draft(
        session_id=session.id,
        user_id=user.id,
        run_id=run.id,
        module="kol-selection",
        business_fields={
            "scope": {
                "category": "美食",
                "platforms": ["小红书"],
                "audience": {"regions": [], "age_ranges": [], "interests": []},
                "filters": {},
            }
        },
        schema_version="kol_selection_v3",
        payload=selection_payload_v1,
        evidence_refs=[],
        artifact_type="kol_selection_v3",
    )
    version_v1 = AgentArtifactVersion(
        artifact_id=sel_artifact.id,
        version=1,
        source_run_id=run.id,
        source_draft_revision_id=sel_rev.id,
        schema_version="kol_selection_v3",
        payload_json=selection_payload_v1,
        # Builder 要求名单 Version 自带 lineage refs（分析 Draft 的 lineage 来源）。
        evidence_refs_json=_selection_refs(selection_payload_v1["data"]["items"]),
        review_json=None,
        data_status="complete",
        created_at=_now(),
    )
    db_session.add(version_v1)
    await db_session.flush()

    # 第一份分析：parent 固定到 V1。
    result1 = await tool.execute(
        ctx,
        BuildKolAnalysisDraftArgs(
            selection_artifact_id=sel_artifact.id, selection_version=1
        ),
    )
    data1 = json.loads(result1.safe_summary)
    assert result1.status == "success"
    assert data1["artifact_key"] == f"kol-analysis:{sel_artifact.id}"

    revisions = (
        await db_session.scalars(
            select(ArtifactDraftRevision).where(ArtifactDraftRevision.artifact_id == data1["artifact_id"])
        )
    ).all()
    rev1 = revisions[0]
    assert rev1.parent_artifact_version_id == version_v1.id

    # 把 rev1 冻结为分析 version 1（parent=V1，原样复制 Revision 的绑定）。
    analysis_version_1 = AgentArtifactVersion(
        artifact_id=data1["artifact_id"],
        version=1,
        source_run_id=run.id,
        source_draft_revision_id=rev1.id,
        parent_artifact_version_id=rev1.parent_artifact_version_id,
        schema_version="kol_analysis_v2",
        payload_json=rev1.payload_json,
        evidence_refs_json=rev1.evidence_refs_json,
        review_json=None,
        data_status="complete",
        created_at=_now(),
    )
    db_session.add(analysis_version_1)
    await db_session.flush()

    # 新的名单版本 V2。
    selection_payload_v2 = _selection_payload([_selection_item("1"), _selection_item("2")])
    version_v2 = AgentArtifactVersion(
        artifact_id=sel_artifact.id,
        version=2,
        source_run_id=run.id,
        source_draft_revision_id=sel_rev.id,
        schema_version="kol_selection_v3",
        payload_json=selection_payload_v2,
        evidence_refs_json=_selection_refs(selection_payload_v2["data"]["items"]),
        review_json=None,
        data_status="complete",
        created_at=_now(),
    )
    db_session.add(version_v2)
    await db_session.flush()

    # 后一批名单 → 复用同一稳定身份，但固定到 V2。
    result2 = await tool.execute(
        ctx,
        BuildKolAnalysisDraftArgs(
            selection_artifact_id=sel_artifact.id, selection_version=2
        ),
    )
    data2 = json.loads(result2.safe_summary)
    assert result2.status == "success"
    assert data2["artifact_id"] == data1["artifact_id"]
    assert data2["artifact_key"] == f"kol-analysis:{sel_artifact.id}"
    assert data2["revision"] == 2

    # 旧版本 parent 绑定不变（不可变）。
    await db_session.flush()
    old_version = await db_session.get(AgentArtifactVersion, analysis_version_1.id)
    assert old_version.parent_artifact_version_id == version_v1.id

    revs_after = (
        await db_session.scalars(
            select(ArtifactDraftRevision)
            .where(ArtifactDraftRevision.artifact_id == data1["artifact_id"])
            .order_by(ArtifactDraftRevision.revision)
        )
    ).all()
    assert [r.revision for r in revs_after] == [1, 2]
    assert revs_after[0].parent_artifact_version_id == version_v1.id
    assert revs_after[1].parent_artifact_version_id == version_v2.id


# ---------------------------------------------------------------------------
# Draft 工具：注册契约 + create/update
# ---------------------------------------------------------------------------


def test_artifact_tools_registration_contract() -> None:
    registry = ToolRegistry()
    entry = registry.register(CreateDraftTool(), category=ARTIFACT_TOOLS)
    assert entry.internal_name == "create_draft"
    assert entry.category == ARTIFACT_TOOLS
    assert entry.points_cost == 0
    assert entry.external_side_effect is True
    assert entry.input_model is CreateDraftArgs
    registry.register(UpdateDraftTool(), category=ARTIFACT_TOOLS)
    assert {e.internal_name for e in registry.registered_tools} == {"create_draft", "update_draft"}


async def test_create_and_update_draft_tools_guard_insight_through_registry(
    db_session, user_factory, session_factory, run_factory,
) -> None:
    """H5 起 insight_board_v1 也属强类型护栏：create/update_draft 直写经
    ToolRegistry 一律 typed_artifact_requires_builder 并回指 build_insight_draft。"""
    user = await user_factory()
    session = await session_factory(user.id)
    run = await run_factory(session.id, user.id)

    registry = ToolRegistry()
    registry.register(CreateDraftTool(db_session), category=ARTIFACT_TOOLS)
    registry.register(UpdateDraftTool(db_session), category=ARTIFACT_TOOLS)

    result = await registry.execute(
        internal_name="create_draft",
        arguments={
            "module": "insight",
            "schema_version": "insight_board_v1",
            "artifact_type": "insight_board_v1",
            "business_fields": {"parent_artifact_version_id": "pv-1", "question": "为什么"},
            "payload": insight_payload(title="初稿"),
            "evidence_refs": [],
        },
        user_id=user.id,
        session_id=session.id,
        run_id=run.id,
        profile=session_analyst,
    )
    assert result.status == "failed"
    assert result.error_type == "typed_artifact_requires_builder"
    assert "build_insight_draft" in result.safe_summary

    # update_draft 直写 insight Draft 同样被拦（经 ArtifactService 落的合法
    # insight Draft 也不放行——修订一律重调 Builder）。
    _, draft, _ = await ArtifactService(db_session).create_or_get_draft(
        session_id=session.id,
        user_id=user.id,
        run_id=run.id,
        module="insight",
        business_fields={"parent_artifact_version_id": "pv-1", "question": "为什么"},
        schema_version="insight_board_v1",
        artifact_type="insight_board_v1",
        payload=insight_payload(title="初稿"),
    )
    updated = await registry.execute(
        internal_name="update_draft",
        arguments={"draft_id": draft.id, "payload": insight_payload(title="修订")},
        user_id=user.id,
        session_id=session.id,
        run_id=run.id,
        profile=session_analyst,
    )
    assert updated.status == "failed"
    assert updated.error_type == "typed_artifact_requires_builder"
    assert "build_insight_draft" in updated.safe_summary


async def test_analysis_builder_writes_parent_binding(
    db_session, user_factory, session_factory, run_factory,
) -> None:
    user = await user_factory()
    session = await session_factory(user.id)
    run = await run_factory(session.id, user.id)
    service = ArtifactService(db_session)
    # H2：kol_analysis_v2 直写 create_draft 已被护栏拒绝，Builder 是唯一落
    # 分析 Draft 的工具路径（parent 绑定语义不变）。
    tool = BuildKolAnalysisDraftTool(db_session)
    ctx = ToolContext(
        user_id=user.id,
        session_id=session.id,
        run_id=run.id,
        profile_name="session_analyst_v1",
    )

    selection_payload = _selection_payload([_selection_item("1")])
    sel_artifact, sel_draft, sel_rev = await service.create_or_get_draft(
        session_id=session.id,
        user_id=user.id,
        run_id=run.id,
        module="kol-selection",
        business_fields={
            "scope": {
                "category": "美食",
                "platforms": ["小红书"],
                "audience": {"regions": [], "age_ranges": [], "interests": []},
                "filters": {},
            }
        },
        schema_version="kol_selection_v3",
        payload=selection_payload,
        evidence_refs=[],
        artifact_type="kol_selection_v3",
    )
    parent_version = AgentArtifactVersion(
        artifact_id=sel_artifact.id,
        version=1,
        source_run_id=run.id,
        source_draft_revision_id=sel_rev.id,
        schema_version="kol_selection_v3",
        payload_json=selection_payload,
        # Builder 要求名单 Version 自带 lineage refs（分析 Draft 的 lineage 来源）。
        evidence_refs_json=_selection_refs(selection_payload["data"]["items"]),
        review_json=None,
        data_status="complete",
        created_at=_now(),
    )
    db_session.add(parent_version)
    await db_session.flush()

    result = await tool.execute(
        ctx,
        BuildKolAnalysisDraftArgs(
            selection_artifact_id=sel_artifact.id, selection_version=1
        ),
    )
    assert result.status == "success"
    data = json.loads(result.safe_summary)

    artifacts = (await db_session.scalars(select(AgentArtifact))).all()
    analysis_artifact = next(a for a in artifacts if a.artifact_key == f"kol-analysis:{sel_artifact.id}")
    assert analysis_artifact.parent_artifact_id == sel_artifact.id
    assert analysis_artifact.artifact_type == "kol_analysis_v2"

    rev = await db_session.get(ArtifactDraftRevision, data["revision_id"])
    assert rev.parent_artifact_version_id == parent_version.id
