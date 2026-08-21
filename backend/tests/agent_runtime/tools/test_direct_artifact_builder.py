"""BuildArtifactDraftTool 模型输入形态测试（提交 1：direct model input contract）。

改造前：模型提交完整 brand_report_v3 发布 payload（含 schema_version/module/
data_status/canonical_data/field_lineage），模型被迫手写 canonical 与 lineage。
改造后：模型只提交业务字段（scope/data/narrative/availability/limitations/
methodology_input），服务器用 typed DTO 校验并确定性组装完整 payload。

覆盖：
- 合法模型输入 → Draft 落库、schema_version==brand_report_v3、零 Evidence 写入；
- 缺失必填字段 → 结构化 draft_build_error（path 指向缺失字段）；
- 提交服务器字段 → server_owned_field_rejected；
- 超大非法输入 → 错误反馈有界（≤2200 字节）且 truncated 标志存在；
- 错误反馈不泄漏提交值（敏感串不回灌）；
- 自纠错：缺字段失败 → 补字段同一 Run 第二次成功；
- contract_not_allowed 保留（模型输入形态的 insight 提交）；
- publish 链路（模型输入 → Draft → 原子发布 → CompletionValidator）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.agent_artifacts.models import AgentArtifactVersion, ArtifactDraftRevision
from app.agent_artifacts.publishing import ArtifactPublicationService
from app.agent_runtime.models import AgentMessage, AgentRun, AgentSession, EvidenceItem
from app.agent_runtime.repository import AgentRunRepository
from app.agent_runtime.tools.builders import BuildArtifactDraftArgs, BuildArtifactDraftTool
from app.agent_runtime.tools.contracts import ToolContext
from app.pi_gateway.completion import CompletionValidator

from tests.agent_artifacts.test_payloads import build_brand_dict


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def brand_model_input() -> dict:
    """最小合法品牌模型输入：复用完整 fixture 的业务字段，去掉服务器字段。"""
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


def insight_model_input() -> dict:
    """模型输入形态的 insight_board_v1（无 schema/module/data_status 等）。"""
    return {
        "title": "品牌钻取",
        "scope": {"summary": "围绕品牌概览"},
        "parent_artifact_id": "parent-artifact-1",
        "narrative": {"summary": "摘要", "findings": []},
        "blocks": [{"block_type": "markdown", "title": "说明", "content": "内容"}],
        "availability": {"blocks": {"status": "complete", "reason_codes": []}},
        "limitations": [],
        "methodology_input": {
            "data_as_of": "2026-01-15T12:00:00",
            "source_names": ["DataTap"],
            "notes": [],
        },
    }


def analysis_report_model_input() -> dict:
    return {
        "title": "长尾营销报告",
        "subject_type": "mixed",
        "scope": {"brand": "示例品牌", "platforms": ["xiaohongshu"]},
        "blocks": [{
            "block_type": "typed_table",
            "id": "creators",
            "title": "达人",
            "columns": [{"key": "name", "label": "名称", "type": "string"}],
            "rows": [["达人 A"]],
        }],
        "fulfillment": [{
            "key": "creators",
            "requested_min": 1,
            "actual_count": 1,
            "status": "complete",
            "reason": "真实返回",
        }],
        "availability": {"blocks": {"status": "complete", "reason_codes": []}},
        "limitations": [],
        "methodology_input": {
            "data_as_of": "2026-01-15T12:00:00",
            "source_names": ["DataTap"],
            "notes": [],
        },
    }


async def _make_run(db_session, user_id: str, session_id: str, *, allow: list[str]) -> AgentRun:
    now = _now()
    run = AgentRun(
        id=str(uuid4()), session_id=session_id, user_id=user_id, profile_name="session_analyst_v1",
        profile_version="v1", model="test-model", runtime_backend="pi", status="running",
        runtime_config_snapshot_json={
            "allowed_artifact_contracts": allow,
            "capability_pack": {"pack_version": "1.0.0", "manifest_digest": "sha256:" + "a" * 64},
            "capability_pack_version": "1.0.0",
            "capability_pack_manifest_digest": "sha256:" + "a" * 64,
        },
        created_at=now, started_at=now, run_kind="user",
    )
    db_session.add(run)
    await db_session.flush()
    return run


async def _make_session(db_session, user_id: str) -> AgentSession:
    now = _now()
    session = AgentSession(
        id=str(uuid4()), user_id=user_id, title="direct artifact", status="active",
        created_at=now, updated_at=now,
    )
    db_session.add(session)
    await db_session.flush()
    return session


def _ctx(user_id: str, session_id: str, run_id: str) -> ToolContext:
    return ToolContext(
        user_id=user_id,
        session_id=session_id,
        run_id=run_id,
        profile_name="session_analyst_v1",
    )


@pytest.mark.asyncio
async def test_direct_builder_accepts_model_input_without_evidence(db_session, user_factory) -> None:
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    run = await _make_run(db_session, user.id, session.id, allow=["brand_report_v3"])

    result = await BuildArtifactDraftTool(db_session).execute(
        _ctx(user.id, session.id, run.id),
        BuildArtifactDraftArgs(artifact_type="brand_report_v3", payload=brand_model_input()),
    )

    assert result.status == "success"
    assert await db_session.scalar(select(EvidenceItem.id).where(EvidenceItem.run_id == run.id)) is None
    revision = await db_session.scalar(select(ArtifactDraftRevision).where(ArtifactDraftRevision.run_id == run.id))
    assert revision is not None
    assert revision.schema_version == "brand_report_v3"
    # 落库 payload 是服务器组装形态：含 canonical 且过强类型校验。
    assert "canonical_data" in revision.payload_json
    assert "field_lineage" in revision.payload_json
    assert revision.payload_json["schema_version"] == "brand_report_v3"


@pytest.mark.asyncio
async def test_direct_builder_missing_required_field_returns_field_path(
    db_session, user_factory
) -> None:
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    run = await _make_run(db_session, user.id, session.id, allow=["brand_report_v3"])

    broken = brand_model_input()
    del broken["data"]["overview"]["total_volume"]
    result = await BuildArtifactDraftTool(db_session).execute(
        _ctx(user.id, session.id, run.id),
        BuildArtifactDraftArgs(artifact_type="brand_report_v3", payload=broken),
    )

    assert result.status == "failed"
    assert result.error_type == "draft_build_error"
    summary = json.loads(result.safe_summary)
    assert "errors" in summary
    assert any(entry["path"] == "/data/overview/total_volume" for entry in summary["errors"])


@pytest.mark.asyncio
async def test_direct_builder_rejects_server_owned_fields(db_session, user_factory) -> None:
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    run = await _make_run(db_session, user.id, session.id, allow=["brand_report_v3"])

    polluted = brand_model_input()
    polluted["schema_version"] = "brand_report_v3"
    result = await BuildArtifactDraftTool(db_session).execute(
        _ctx(user.id, session.id, run.id),
        BuildArtifactDraftArgs(artifact_type="brand_report_v3", payload=polluted),
    )

    assert result.status == "failed"
    assert result.error_type == "server_owned_field_rejected"
    summary = json.loads(result.safe_summary)
    assert any(field["path"] == "/schema_version" for field in summary["server_owned_fields"])


@pytest.mark.asyncio
async def test_direct_builder_error_feedback_is_bounded(db_session, user_factory) -> None:
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    run = await _make_run(db_session, user.id, session.id, allow=["brand_report_v3"])

    invalid = brand_model_input()
    for index in range(30):
        invalid[f"bogus_field_{index}"] = index
    result = await BuildArtifactDraftTool(db_session).execute(
        _ctx(user.id, session.id, run.id),
        BuildArtifactDraftArgs(artifact_type="brand_report_v3", payload=invalid),
    )

    assert result.status == "failed"
    assert len(result.safe_summary.encode("utf-8")) <= 2200
    summary = json.loads(result.safe_summary)
    assert summary["truncated"] is True
    assert summary["total_errors"] >= 30
    assert len(summary["errors"]) <= 8


@pytest.mark.asyncio
async def test_direct_builder_error_feedback_does_not_leak_submitted_values(
    db_session, user_factory
) -> None:
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    run = await _make_run(db_session, user.id, session.id, allow=["brand_report_v3"])

    secret = "sk-proj-SECRETVALUE123"
    broken = brand_model_input()
    broken["narrative"]["executive_summary"] = f"摘要含敏感 {secret}"
    del broken["data"]["overview"]["total_volume"]
    result = await BuildArtifactDraftTool(db_session).execute(
        _ctx(user.id, session.id, run.id),
        BuildArtifactDraftArgs(artifact_type="brand_report_v3", payload=broken),
    )

    assert result.status == "failed"
    assert secret not in result.safe_summary


@pytest.mark.asyncio
async def test_direct_builder_self_corrects_on_same_run(db_session, user_factory) -> None:
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    run = await _make_run(db_session, user.id, session.id, allow=["brand_report_v3"])
    tool = BuildArtifactDraftTool(db_session)
    ctx = _ctx(user.id, session.id, run.id)

    broken = brand_model_input()
    del broken["data"]["overview"]["total_volume"]
    first = await tool.execute(
        ctx, BuildArtifactDraftArgs(artifact_type="brand_report_v3", payload=broken)
    )
    assert first.status == "failed"

    second = await tool.execute(
        ctx,
        BuildArtifactDraftArgs(artifact_type="brand_report_v3", payload=brand_model_input()),
    )
    assert second.status == "success"
    revision = await db_session.scalar(select(ArtifactDraftRevision).where(ArtifactDraftRevision.run_id == run.id))
    assert revision is not None
    assert revision.schema_version == "brand_report_v3"


@pytest.mark.asyncio
async def test_direct_builder_rejects_contract_not_in_snapshot(db_session, user_factory) -> None:
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    run = await _make_run(db_session, user.id, session.id, allow=["brand_report_v3"])

    result = await BuildArtifactDraftTool(db_session).execute(
        _ctx(user.id, session.id, run.id),
        BuildArtifactDraftArgs(artifact_type="insight_board_v1", payload=insight_model_input()),
    )

    assert result.status == "failed"
    assert result.error_type == "artifact_contract_not_allowed"


@pytest.mark.asyncio
async def test_direct_builder_accepts_analysis_report_without_evidence(db_session, user_factory) -> None:
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    run = await _make_run(db_session, user.id, session.id, allow=["analysis_report_v1"])

    result = await BuildArtifactDraftTool(db_session).execute(
        _ctx(user.id, session.id, run.id),
        BuildArtifactDraftArgs(
            artifact_type="analysis_report_v1",
            payload=analysis_report_model_input(),
        ),
    )

    assert result.status == "success"
    revision = await db_session.scalar(
        select(ArtifactDraftRevision).where(ArtifactDraftRevision.run_id == run.id)
    )
    assert revision is not None
    assert revision.schema_version == "analysis_report_v1"
    assert revision.payload_json["module"] == "report"
    assert revision.payload_json["blocks"][0]["rows"] == [["达人 A"]]


@pytest.mark.asyncio
async def test_direct_builder_publishes_without_fabricated_evidence(
    db_session, user_factory
) -> None:
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    run = await _make_run(db_session, user.id, session.id, allow=["brand_report_v3"])
    assert await AgentRunRepository(db_session).claim_lease(run.id, "direct-worker", 300)

    result = await BuildArtifactDraftTool(db_session).execute(
        _ctx(user.id, session.id, run.id),
        BuildArtifactDraftArgs(artifact_type="brand_report_v3", payload=brand_model_input()),
    )
    assert result.status == "success"
    draft_id = (
        await db_session.scalar(select(ArtifactDraftRevision).where(ArtifactDraftRevision.run_id == run.id))
    ).draft_id
    published = await ArtifactPublicationService(db_session).publish(
        run_id=run.id, draft_ids=(draft_id,), worker_id="direct-worker"
    )
    assert published[0].status == "published"
    version = await db_session.scalar(
        select(AgentArtifactVersion).where(AgentArtifactVersion.source_run_id == run.id)
    )
    assert version is not None
    assert version.evidence_refs_json == []
    assert version.lineage_snapshot_json == {"mode": "model_direct_v1", "refs": [], "source_tool_call_ids": []}
    assert await db_session.scalar(select(EvidenceItem.id).where(EvidenceItem.run_id == run.id)) is None
    now = _now()
    db_session.add(
        AgentMessage(
            id=str(uuid4()), session_id=session.id, run_id=run.id, role="assistant",
            content="报告已生成", metadata_json={}, sequence=1, created_at=now,
        )
    )
    await db_session.flush()
    completion = await CompletionValidator(db_session).validate(run)
    assert completion.ok
    assert completion.artifact_version_id == version.id
