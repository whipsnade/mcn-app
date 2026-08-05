"""create_draft / update_draft 强类型直写护栏（H2 + H5，第四轮 UAT 回归）。

第四轮 UAT 取证：kol_detail_v1 Run 用 ``create_draft`` 手写了一份形态错误的
kol_detail_v2 payload，连续 3 次 ``artifact_payload_invalid`` 后放弃交付；
开放式钻取场景模型手写 insight_board_v1 同样连续失败（字段级错误回喂也收敛
不了）。设计 §6.1 明确六类强类型正式 Artifact（brand_report_v3 /
campaign_report_v2 / kol_selection_v3 / kol_analysis_v2 / kol_detail_v2 /
insight_board_v1）必须走对应 ``build_*`` Builder——直写在工具层直接拒绝并
回指 Builder，不再让模型在手写 payload 上消耗重试。Builder 路径（含
create_or_get 再构建 = 追加新 Revision）不被护栏拦截。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.agent_artifacts.models import AgentArtifact, ArtifactDraft
from app.agent_artifacts.service import ArtifactService
from app.agent_runtime.tools.artifacts import (
    CreateDraftArgs,
    CreateDraftTool,
    UpdateDraftArgs,
    UpdateDraftTool,
)
from app.agent_runtime.tools.builders import BuildBrandReportDraftTool

from tests.agent_artifacts.payload_fixtures import insight_payload
from tests.agent_runtime.tools.test_builders import (
    BRAND_SCOPE,
    _brand_evidence_rows,
    _ctx,
    _make_run,
    _make_session,
    _write_evidence,
)

GUARD_ERROR_TYPE = "typed_artifact_requires_builder"

# 六类强类型：module → (schema_version, business_fields, 应回指的 Builder 工具)。
_TYPED_CASES = (
    ("brand", "brand_report_v3", {"brand": "瑞幸咖啡"}, "build_brand_report_draft"),
    (
        "campaign",
        "campaign_report_v2",
        {"brand": "瑞幸咖啡", "campaign": "生椰拿铁上新"},
        "build_campaign_report_draft",
    ),
    (
        "kol-selection",
        "kol_selection_v3",
        {"scope": {"platforms": ["xiaohongshu"]}},
        "build_kol_selection_draft",
    ),
    (
        "kol-analysis",
        "kol_analysis_v2",
        {"selection_artifact_id": "artifact-1"},
        "build_kol_analysis_draft",
    ),
    (
        "kol-detail",
        "kol_detail_v2",
        {"platform": "xiaohongshu", "kol_uid": "kol-1"},
        "build_kol_detail_draft",
    ),
    (
        "insight",
        "insight_board_v1",
        {"parent_artifact_version_id": "pv-1", "question": "按平台钻取？"},
        "build_insight_draft",
    ),
)

_INSIGHT_BUSINESS_FIELDS = {"parent_artifact_version_id": "pv-1", "question": "按平台钻取？"}


@pytest.mark.parametrize(
    ("module", "schema_version", "business_fields", "builder_name"),
    _TYPED_CASES,
    ids=[case[0] for case in _TYPED_CASES],
)
async def test_create_draft_rejects_typed_schemas(
    db_session, user_factory, module, schema_version, business_fields, builder_name
) -> None:
    """六类强类型 schema 直写 create_draft：结构化拒绝并点名对应 Builder。"""
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    run, step = await _make_run(db_session, session.id, user.id)

    tool = CreateDraftTool(db_session)
    result = await tool.execute(
        _ctx(user.id, session.id, run.id, step.id),
        CreateDraftArgs(
            module=module,
            schema_version=schema_version,
            artifact_type=schema_version,
            business_fields=business_fields,
            payload={},  # 护栏先于 payload 校验触发，空 payload 足以验证拦截
        ),
    )

    assert result.status == "failed"
    assert result.error_type == GUARD_ERROR_TYPE
    assert builder_name in result.safe_summary
    assert schema_version in result.safe_summary
    # 拦截即返回：不落任何 Artifact 行。
    artifact_count = await db_session.scalar(
        select(func.count(AgentArtifact.id)).where(AgentArtifact.session_id == session.id)
    )
    assert artifact_count == 0


async def test_create_draft_rejects_insight_board(db_session, user_factory) -> None:
    """insight_board_v1 同样禁止裸写（H5）：护栏回指 build_insight_draft。"""
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    run, step = await _make_run(db_session, session.id, user.id)

    tool = CreateDraftTool(db_session)
    result = await tool.execute(
        _ctx(user.id, session.id, run.id, step.id),
        CreateDraftArgs(
            module="insight",
            schema_version="insight_board_v1",
            artifact_type="insight_board_v1",
            business_fields=dict(_INSIGHT_BUSINESS_FIELDS),
            payload=insight_payload(title="钻取初稿"),
        ),
    )

    assert result.status == "failed"
    assert result.error_type == GUARD_ERROR_TYPE
    assert "build_insight_draft" in result.safe_summary
    assert "insight_board_v1" in result.safe_summary


@pytest.mark.parametrize(
    ("module", "schema_version", "business_fields", "builder_name"),
    _TYPED_CASES,
    ids=[case[0] for case in _TYPED_CASES],
)
async def test_update_draft_rejects_typed_drafts(
    db_session, user_factory, module, schema_version, business_fields, builder_name
) -> None:
    """六类 typed Draft 的 update_draft 直写同样被拦截（修订走 Builder 重建）。"""
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    run, step = await _make_run(db_session, session.id, user.id)

    # 护栏只按 Artifact module 判定，与 payload 内容无关：直接落最小 ORM 行，
    # 不必为五类各构造一份合法强类型 payload。
    now = datetime.now(UTC).replace(tzinfo=None)
    artifact = AgentArtifact(
        id=str(uuid4()),
        session_id=session.id,
        user_id=user.id,
        module=module,
        artifact_type=schema_version,
        artifact_key=f"key-{module}-{uuid4()}",
        status="draft",
        latest_version=0,
        activity_sequence=0,
        created_at=now,
        updated_at=now,
    )
    db_session.add(artifact)
    await db_session.flush()
    draft = ArtifactDraft(
        id=str(uuid4()),
        artifact_id=artifact.id,
        session_id=session.id,
        owner_run_id=run.id,
        current_revision=1,
        status="drafting",
        review_count=0,
        revision_count=1,
        updated_at=now,
    )
    db_session.add(draft)
    await db_session.flush()

    tool = UpdateDraftTool(db_session)
    result = await tool.execute(
        _ctx(user.id, session.id, run.id, step.id),
        UpdateDraftArgs(draft_id=draft.id, payload={}),
    )

    assert result.status == "failed"
    assert result.error_type == GUARD_ERROR_TYPE
    assert builder_name in result.safe_summary


async def test_update_draft_rejects_insight_board(db_session, user_factory) -> None:
    """insight_board_v1 Draft 的修订同样禁止裸写（H5）：护栏回指 build_insight_draft。"""
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    run, step = await _make_run(db_session, session.id, user.id)

    _artifact, draft, _revision = await ArtifactService(db_session).create_or_get_draft(
        session_id=session.id,
        user_id=user.id,
        run_id=run.id,
        module="insight",
        business_fields=dict(_INSIGHT_BUSINESS_FIELDS),
        schema_version="insight_board_v1",
        artifact_type="insight_board_v1",
        payload=insight_payload(title="钻取初稿"),
    )

    tool = UpdateDraftTool(db_session)
    result = await tool.execute(
        _ctx(user.id, session.id, run.id, step.id),
        UpdateDraftArgs(draft_id=draft.id, payload=insight_payload(title="钻取修订")),
    )

    assert result.status == "failed"
    assert result.error_type == GUARD_ERROR_TYPE
    assert "build_insight_draft" in result.safe_summary


async def test_typed_draft_builder_rebuild_not_blocked(db_session, user_factory) -> None:
    """Builder 路径不动：Builder 创建 typed Draft 后，update_draft 直写被拦，
    但同身份再调 Builder 正常追加新 Revision（create_or_get 语义）。"""
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    run, step = await _make_run(db_session, session.id, user.id)
    ctx = _ctx(user.id, session.id, run.id, step.id)
    evidence_args = {}
    for group, rows in _brand_evidence_rows().items():
        evidence_args[group] = [
            await _write_evidence(
                db_session,
                session_id=session.id,
                run_id=run.id,
                step_id=step.id,
                payload=rows,
            )
        ]

    builder = BuildBrandReportDraftTool(db_session)
    args = {"scope": BRAND_SCOPE, "evidence": evidence_args}
    first = json.loads((await builder.execute(ctx, args)).safe_summary)

    # update_draft 直写该 typed Draft：被护栏拒绝并回指 Builder。
    direct = await UpdateDraftTool(db_session).execute(
        ctx, UpdateDraftArgs(draft_id=first["draft_id"], payload={})
    )
    assert direct.status == "failed"
    assert direct.error_type == GUARD_ERROR_TYPE
    assert "build_brand_report_draft" in direct.safe_summary

    # Builder 再构建：同一 Artifact 追加新 Revision，不受护栏影响。
    second = json.loads((await builder.execute(ctx, args)).safe_summary)
    assert second["artifact_id"] == first["artifact_id"]
    assert second["draft_id"] == first["draft_id"]
    assert second["revision"] == first["revision"] + 1
