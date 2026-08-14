"""``build_insight_draft`` Builder 工具测试（H5：开放式钻取看板收口）。

覆盖：
- 端到端：父 Version 已发布 → 三来源 value_ref 解析复制真实值 → Draft 落库
  且 payload 过强类型校验、数字级 lineage 自动生成；
- 父 Version 不存在 / 跨 Session → not_found（不泄漏存在性）；
- value_ref 不可解析 / 跨 Session Evidence / 计算调用未 settled → 结构化
  ``draft_build_error`` 字段级回喂；
- 第 9 种 Block 类型、多余字段、双包嵌套、裸数字字面值 → 字段级拒绝；
- 同身份重调（Reviewer revise 后）→ 同一 Artifact 追加新 Revision；
- 工厂装配：注册进 ARTIFACT_TOOLS（见 test_builders 的工厂用例，工具名
  已并入 BUILDER_TOOL_NAMES）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select

from app.agent_artifacts.models import (
    AgentArtifact,
    AgentArtifactVersion,
    ArtifactDraftRevision,
)
from app.agent_artifacts.payloads.insight import InsightBoardV1
from app.agent_artifacts.service import ArtifactService
from app.agent_runtime.models import AgentStep, AgentToolCall
from app.agent_runtime.tools.builders import BuildInsightDraftTool

from tests.agent_artifacts.payload_fixtures import brand_payload
from tests.agent_runtime.tools.test_builders import (
    _ctx,
    _make_run,
    _make_session,
    _now,
    _write_evidence,
)

PARENT_BRAND = "瑞幸咖啡"


async def _publish_parent_brand(db_session, session, run, user) -> tuple[Any, Any]:
    """落一个已发布的 brand_report_v3 Version 作为钻取父级（模拟发布结果）。"""
    artifact, _draft, revision = await ArtifactService(db_session).create_or_get_draft(
        session_id=session.id,
        user_id=user.id,
        run_id=run.id,
        module="brand",
        business_fields={"brand": PARENT_BRAND},
        schema_version="brand_report_v3",
        artifact_type="brand_report_v3",
        payload=brand_payload(),
    )
    version = AgentArtifactVersion(
        id=str(uuid4()),
        artifact_id=artifact.id,
        version=1,
        source_run_id=run.id,
        source_draft_revision_id=revision.id,
        schema_version=revision.schema_version,
        payload_json=revision.payload_json,
        evidence_refs_json=revision.evidence_refs_json,
        data_status=revision.payload_json["data_status"],
        created_at=_now(),
    )
    db_session.add(version)
    artifact.latest_version = 1
    artifact.status = "published"
    await db_session.flush()
    return artifact, version


async def _settled_calculation_call(
    db_session, run, *, result: Any, status: str = "settled", service: str = "internal"
) -> AgentToolCall:
    """落一个内部计算工具调用行：结果经 step.output_json.safe_summary 持久化。"""
    now = datetime.now(UTC).replace(tzinfo=None)
    attempt_id = await db_session.scalar(
        select(AgentStep.attempt_id).where(AgentStep.run_id == run.id).limit(1)
    )
    next_sequence = (
        await db_session.scalar(
            select(func.count(AgentStep.id)).where(AgentStep.run_id == run.id)
        )
        or 0
    ) + 1
    step = AgentStep(
        id=str(uuid4()),
        run_id=run.id,
        attempt_id=attempt_id,
        sequence=next_sequence,
        step_type="tool_call",
        status="completed",
        created_at=now,
        output_json={
            "status": "success",
            "safe_summary": json.dumps(result, ensure_ascii=False),
        },
    )
    db_session.add(step)
    await db_session.flush()
    call = AgentToolCall(
        id=str(uuid4()),
        run_id=run.id,
        step_id=step.id,
        logical_call_id=f"call-{uuid4()}",
        service=service,
        internal_tool_name="calculate_expression",
        arguments_json={},
        arguments_hash="h",
        status=status,
        points_reserved=0,
        points_settled=0,
        started_at=now,
        completed_at=now,
    )
    db_session.add(call)
    await db_session.flush()
    return call


def _insight_args(
    parent_version_id: str,
    *,
    evidence_id: str,
    calculation_call_id: str,
    question: str = "分平台声量对比？",
) -> dict[str, Any]:
    """覆盖 8 型 Block 与 value_ref 三来源的完整合法入参。"""
    return {
        "parent_artifact_version_id": parent_version_id,
        "question": question,
        "title": "分平台钻取",
        "scope": {"summary": "按平台钻取", "brand": PARENT_BRAND},
        "blocks": [
            {
                "type": "metric_grid",
                "title": "核心指标",
                "cards": [
                    {
                        "key": "total_volume",
                        "label": "声量",
                        "unit": "条",
                        "value_ref": {
                            "source_type": "evidence",
                            "evidence_id": evidence_id,
                            "source_path": "/0/声量",
                        },
                    }
                ],
            },
            {
                "type": "table",
                "title": "分平台",
                "columns": ["平台", "声量"],
                "rows": [
                    [
                        "小红书",
                        {
                            "value_ref": {
                                "source_type": "evidence",
                                "evidence_id": evidence_id,
                                "source_path": "/0/声量",
                            }
                        },
                    ]
                ],
            },
            {
                "type": "bar_chart",
                "title": "平台对比",
                "categories": ["总量"],
                "series": [
                    {
                        "name": "声量",
                        "values": [
                            {
                                "source_type": "artifact",
                                "artifact_version_id": parent_version_id,
                                "source_path": "/data/overview/total_volume",
                            }
                        ],
                    }
                ],
            },
            {
                "type": "line_chart",
                "title": "趋势",
                "x_labels": ["2026-07-01"],
                "series": [
                    {
                        "name": "声量",
                        "values": [
                            {
                                "source_type": "evidence",
                                "evidence_id": evidence_id,
                                "source_path": "/0/声量",
                            }
                        ],
                    }
                ],
            },
            {
                "type": "pie_chart",
                "title": "情感占比",
                "slices": [
                    {
                        "name": "正面",
                        "value_ref": {
                            "source_type": "calculation",
                            "tool_call_id": calculation_call_id,
                            "result_path": "/value",
                            "input_refs": [
                                {
                                    "source_type": "evidence",
                                    "evidence_id": evidence_id,
                                    "source_path": "/0/声量",
                                }
                            ],
                        },
                    }
                ],
            },
            {"type": "markdown", "title": "结论", "content": "声量集中在小红书。"},
            {
                "type": "timeline",
                "title": "节奏",
                "items": [{"date": "2026-07-01", "title": "上新"}],
            },
            {
                "type": "references",
                "title": "来源",
                "items": [{"label": "原帖", "url": "https://example.com/p/1"}],
            },
        ],
        "narrative": {
            "summary": "小红书声量占绝对主导。",
            "findings": [
                {
                    "title": "平台集中",
                    "detail": "小红书声量 100。",
                    "supporting_paths": ["data.0.cards.0.value"],
                }
            ],
        },
    }


async def _setup(db_session, user_factory):
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    run, step = await _make_run(db_session, session.id, user.id)
    ctx = _ctx(user.id, session.id, run.id, step.id)
    parent_artifact, parent_version = await _publish_parent_brand(
        db_session, session, run, user
    )
    evidence_id = await _write_evidence(
        db_session,
        session_id=session.id,
        run_id=run.id,
        step_id=step.id,
        payload=[{"平台": "小红书", "声量": 100}],
    )
    calc_call = await _settled_calculation_call(db_session, run, result={"value": 42})
    return user, session, run, ctx, parent_artifact, parent_version, evidence_id, calc_call


async def _latest_revision_payload(db_session, artifact_id: str) -> dict[str, Any]:
    revision = await db_session.scalar(
        select(ArtifactDraftRevision)
        .where(ArtifactDraftRevision.artifact_id == artifact_id)
        .order_by(ArtifactDraftRevision.revision.desc())
        .limit(1)
    )
    assert revision is not None
    return revision.payload_json


async def test_build_insight_draft_tool_end_to_end(db_session, user_factory) -> None:
    (
        user,
        session,
        run,
        ctx,
        parent_artifact,
        parent_version,
        evidence_id,
        calc_call,
    ) = await _setup(db_session, user_factory)

    tool = BuildInsightDraftTool(db_session)
    result = await tool.execute(
        ctx, _insight_args(parent_version.id, evidence_id=evidence_id, calculation_call_id=calc_call.id)
    )
    assert result.status == "success", result.safe_summary
    summary = json.loads(result.safe_summary)

    # 输出契约：身份 + 摘要，绝不回灌完整 payload。
    for key in ("artifact_id", "artifact_key", "draft_id", "revision_id", "revision"):
        assert key in summary
    assert summary["schema_version"] == "insight_board_v1"
    assert "block_type" not in result.safe_summary
    assert summary["artifact_key"].startswith(f"insight:{parent_version.id}:")

    payload = await _latest_revision_payload(db_session, summary["artifact_id"])
    InsightBoardV1.model_validate(payload)
    assert payload["parent_artifact_id"] == parent_artifact.id
    assert payload["module"] == "brand"
    assert payload["data_status"] == "complete"

    data = payload["data"]
    # value_ref 三来源解析并复制真实值。
    assert data[0]["cards"][0]["value"] == 100  # evidence
    assert data[1]["rows"][0] == ["小红书", 100]  # evidence（table 数字单元格）
    assert data[2]["series"][0]["values"] == [1000]  # 父 Version payload
    assert data[3]["series"][0]["values"] == [100]  # evidence
    assert data[4]["slices"][0]["value"] == 42  # settled 计算调用结果

    # 数字级 lineage 自动生成（RFC6901 artifact_path + sources + derivation）。
    refs = {ref["artifact_path"]: ref for ref in (await _latest_revision(db_session, summary))}
    assert refs["/data/0/cards/0/value"]["sources"] == [
        {"source_type": "evidence", "evidence_id": evidence_id, "source_path": "/0/声量"}
    ]
    assert refs["/data/2/series/0/values/0"]["sources"] == [
        {
            "source_type": "artifact",
            "artifact_version_id": parent_version.id,
            "source_path": "/data/overview/total_volume",
        }
    ]
    calc_ref = refs["/data/4/slices/0/value"]
    assert calc_ref["derivation"]["tool_call_id"] == calc_call.id
    assert calc_ref["derivation"]["method"] == "calculate_expression"
    assert calc_ref["derivation"]["input_paths"] == ["/0/声量"]

    # 子 Artifact 固定到父 Artifact / 父 Version（不可变绑定）。
    artifact = await db_session.get(AgentArtifact, summary["artifact_id"])
    assert artifact is not None
    assert artifact.parent_artifact_id == parent_artifact.id
    revision = await db_session.get(ArtifactDraftRevision, summary["revision_id"])
    assert revision is not None
    assert revision.parent_artifact_version_id == parent_version.id


async def _latest_revision(db_session, summary: dict[str, Any]) -> list[dict[str, Any]]:
    revision = await db_session.get(ArtifactDraftRevision, summary["revision_id"])
    assert revision is not None
    return revision.evidence_refs_json or []


async def test_build_insight_draft_tool_rebuild_appends_revision(db_session, user_factory) -> None:
    """Reviewer revise 后重调本工具：同一 Artifact 追加新 Revision。"""
    (
        _user,
        _session,
        _run,
        ctx,
        _pa,
        parent_version,
        evidence_id,
        calc_call,
    ) = await _setup(db_session, user_factory)
    args = _insight_args(
        parent_version.id, evidence_id=evidence_id, calculation_call_id=calc_call.id
    )

    tool = BuildInsightDraftTool(db_session)
    first = json.loads((await tool.execute(ctx, args)).safe_summary)
    # revise 后改叙事重调：稳定身份复用（question + parent version 未变）。
    args["narrative"]["summary"] = "修订后的结论。"
    second_result = await tool.execute(ctx, args)
    assert second_result.status == "success", second_result.safe_summary
    second = json.loads(second_result.safe_summary)
    assert second["artifact_id"] == first["artifact_id"]
    assert second["draft_id"] == first["draft_id"]
    assert second["revision"] == first["revision"] + 1

    payload = await _latest_revision_payload(db_session, first["artifact_id"])
    assert payload["narrative"]["summary"] == "修订后的结论。"


async def test_build_insight_draft_tool_parent_version_not_found(
    db_session, user_factory
) -> None:
    user = await user_factory()
    session = await _make_session(db_session, user.id)
    run, step = await _make_run(db_session, session.id, user.id)
    ctx = _ctx(user.id, session.id, run.id, step.id)
    evidence_id = await _write_evidence(
        db_session, session_id=session.id, run_id=run.id, step_id=step.id, payload=[{"声量": 1}]
    )
    calc_call = await _settled_calculation_call(db_session, run, result={"value": 1})

    tool = BuildInsightDraftTool(db_session)
    result = await tool.execute(
        ctx,
        _insight_args("version-bogus", evidence_id=evidence_id, calculation_call_id=calc_call.id),
    )
    assert result.status == "failed"
    assert result.error_type == "not_found"


async def test_build_insight_draft_tool_parent_version_cross_session(
    db_session, user_factory
) -> None:
    """父 Version 属于其他 Session：一律 not_found，不泄漏存在性。"""
    user, session, run, ctx, _pa, _pv, evidence_id, calc_call = await _setup(
        db_session, user_factory
    )
    other_session = await _make_session(db_session, user.id)
    other_run, _step = await _make_run(db_session, other_session.id, user.id)
    _oa, other_version = await _publish_parent_brand(db_session, other_session, other_run, user)

    tool = BuildInsightDraftTool(db_session)
    result = await tool.execute(
        ctx,
        _insight_args(other_version.id, evidence_id=evidence_id, calculation_call_id=calc_call.id),
    )
    assert result.status == "failed"
    assert result.error_type == "not_found"


async def test_build_insight_draft_tool_unresolvable_value_ref_field_level(
    db_session, user_factory
) -> None:
    """source_path 不可解析：结构化 draft_build_error 指明出错板块位置。"""
    (
        _user,
        _session,
        _run,
        ctx,
        _pa,
        parent_version,
        evidence_id,
        calc_call,
    ) = await _setup(db_session, user_factory)
    args = _insight_args(
        parent_version.id, evidence_id=evidence_id, calculation_call_id=calc_call.id
    )
    args["blocks"][0]["cards"][0]["value_ref"]["source_path"] = "/0/不存在的字段"

    result = await BuildInsightDraftTool(db_session).execute(ctx, args)
    assert result.status == "failed"
    assert result.error_type == "draft_build_error"
    assert "blocks.0.cards.0" in result.safe_summary


async def test_build_insight_draft_tool_cross_session_evidence_rejected(
    db_session, user_factory
) -> None:
    user, session, run, ctx, _pa, parent_version, _ev, calc_call = await _setup(
        db_session, user_factory
    )
    other_session = await _make_session(db_session, user.id)
    other_run, other_step = await _make_run(db_session, other_session.id, user.id)
    foreign_evidence = await _write_evidence(
        db_session,
        session_id=other_session.id,
        run_id=other_run.id,
        step_id=other_step.id,
        payload=[{"声量": 999}],
    )
    args = _insight_args(
        parent_version.id, evidence_id=foreign_evidence, calculation_call_id=calc_call.id
    )

    result = await BuildInsightDraftTool(db_session).execute(ctx, args)
    assert result.status == "failed"
    assert result.error_type == "draft_build_error"
    assert "evidence not found" in result.safe_summary


async def test_build_insight_draft_tool_unknown_block_type_rejected(
    db_session, user_factory
) -> None:
    """第 9 种 Block 类型：字段级拒绝并点名判别字段。"""
    (
        _user,
        _session,
        _run,
        ctx,
        _pa,
        parent_version,
        evidence_id,
        calc_call,
    ) = await _setup(db_session, user_factory)
    args = _insight_args(
        parent_version.id, evidence_id=evidence_id, calculation_call_id=calc_call.id
    )
    args["blocks"][0] = {"type": "scatter_chart", "title": "x", "points": []}

    result = await BuildInsightDraftTool(db_session).execute(ctx, args)
    assert result.status == "failed"
    assert result.error_type == "draft_build_error"
    assert "scatter_chart" in result.safe_summary


async def test_build_insight_draft_tool_common_model_mistakes_field_level(
    db_session, user_factory
) -> None:
    """模型常见错误（多余字段 / 双包嵌套 / 裸数字字面值）→ 字段级回喂。"""
    (
        _user,
        _session,
        _run,
        ctx,
        _pa,
        parent_version,
        evidence_id,
        calc_call,
    ) = await _setup(db_session, user_factory)
    base = _insight_args(
        parent_version.id, evidence_id=evidence_id, calculation_call_id=calc_call.id
    )
    tool = BuildInsightDraftTool(db_session)

    # 多余字段（metric_grid.metrics 而非 cards，且编造键）。
    args = json.loads(json.dumps(base))
    args["blocks"][0] = {
        "type": "metric_grid",
        "title": "核心指标",
        "metrics": [{"key": "v", "label": "声量", "value": 100}],
    }
    result = await tool.execute(ctx, args)
    assert result.status == "failed"
    assert result.error_type == "draft_build_error"
    assert "metrics" in result.safe_summary

    # 双包嵌套（card 外面再包一层）。
    args = json.loads(json.dumps(base))
    args["blocks"][0]["cards"] = [
        {"card": {"key": "v", "label": "声量", "value_ref": {"source_type": "evidence"}}}
    ]
    result = await tool.execute(ctx, args)
    assert result.status == "failed"
    assert result.error_type == "draft_build_error"
    assert "cards.0" in result.safe_summary

    # 裸数字字面值：series 数字必须经 value_ref 引用，不允许直接填值。
    args = json.loads(json.dumps(base))
    args["blocks"][2]["series"][0]["values"] = [1000]
    result = await tool.execute(ctx, args)
    assert result.status == "failed"
    assert result.error_type == "draft_build_error"
    assert "values.0" in result.safe_summary

    # table 数字单元格同理：裸数字拒绝。
    args = json.loads(json.dumps(base))
    args["blocks"][1]["rows"] = [["小红书", 100]]
    result = await tool.execute(ctx, args)
    assert result.status == "failed"
    assert result.error_type == "draft_build_error"
    assert "rows.0.1" in result.safe_summary


async def test_build_insight_draft_tool_calculation_call_not_settled_rejected(
    db_session, user_factory
) -> None:
    """计算来源必须指向当前 Session 已 settled 的内部计算调用。"""
    user, session, run, ctx, _pa, parent_version, evidence_id, _cc = await _setup(
        db_session, user_factory
    )
    failed_call = await _settled_calculation_call(db_session, run, result={"value": 1}, status="failed")
    args = _insight_args(
        parent_version.id, evidence_id=evidence_id, calculation_call_id=failed_call.id
    )
    result = await BuildInsightDraftTool(db_session).execute(ctx, args)
    assert result.status == "failed"
    assert result.error_type == "draft_build_error"
    assert "settled" in result.safe_summary

    # 非 internal（MCP）调用同样拒绝。
    mcp_call = await _settled_calculation_call(db_session, run, result={"value": 1}, service="mcp")
    args = _insight_args(
        parent_version.id, evidence_id=evidence_id, calculation_call_id=mcp_call.id
    )
    result = await BuildInsightDraftTool(db_session).execute(ctx, args)
    assert result.status == "failed"
    assert result.error_type == "draft_build_error"
    assert "internal" in result.safe_summary


async def test_build_insight_draft_tool_non_numeric_series_value_rejected(
    db_session, user_factory
) -> None:
    """图表数值必须解析为数字：value_ref 指向字符串字段时字段级拒绝。"""
    (
        _user,
        _session,
        _run,
        ctx,
        _pa,
        parent_version,
        evidence_id,
        calc_call,
    ) = await _setup(db_session, user_factory)
    args = _insight_args(
        parent_version.id, evidence_id=evidence_id, calculation_call_id=calc_call.id
    )
    args["blocks"][2]["series"][0]["values"] = [
        {
            "source_type": "evidence",
            "evidence_id": evidence_id,
            "source_path": "/0/平台",  # 字符串 "小红书"
        }
    ]
    result = await BuildInsightDraftTool(db_session).execute(ctx, args)
    assert result.status == "failed"
    assert result.error_type == "draft_build_error"
    assert "blocks.2.series.0" in result.safe_summary


async def test_build_insight_draft_tool_narrative_validated(db_session, user_factory) -> None:
    """narrative 的 supporting_paths 必须指向 data 内真实路径。"""
    (
        _user,
        _session,
        _run,
        ctx,
        _pa,
        parent_version,
        evidence_id,
        calc_call,
    ) = await _setup(db_session, user_factory)
    args = _insight_args(
        parent_version.id, evidence_id=evidence_id, calculation_call_id=calc_call.id
    )
    args["narrative"]["findings"][0]["supporting_paths"] = ["data.bogus.path"]
    result = await BuildInsightDraftTool(db_session).execute(ctx, args)
    assert result.status == "failed"
    assert result.error_type == "draft_build_error"
