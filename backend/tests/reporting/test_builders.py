"""品牌/活动报告构建器：证据聚合 → 模型撰写 → 会话级落库。

品牌路径已演进 v2（Task 6）：assemble_brand_report 代码组装快照 →
build_brand_narrative 模型叙事 → 兼容 ReportDocument 一次落库
（payload_json + template_version=brand_report_v2）；活动路径保持
campaign_analysis_v1 模型直出 ReportDocument 不变。
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
from types import SimpleNamespace
from uuid import uuid4

from pydantic import ValidationError
import pytest
from sqlalchemy import func, select

from app.model.contracts import (
    ModelAdapterError,
    ModelPlanInvalidError,
    StructuredResult,
)
from app.reporting.blocks import (
    ChartBlock,
    ChartSeries,
    HeadingBlock,
    MarkdownBlock,
    MetricGridBlock,
    MetricItem,
    ReportDocument,
    TableBlock,
    TagListBlock,
)
from app.reporting.brand_assembler import assemble_brand_report
from app.reporting.brand_payload import BrandReportNarrative, BrandReportPayload
from app.reporting.builders import (
    build_brand_compat_document,
    collect_goal_evidence,
    run_brand_analysis,
    run_campaign_analysis,
)
from app.reporting.models import AnalysisReport
from app.workspace.models import WorkspaceSession


def _narrative_output() -> dict:
    return {
        "praise_points": ["新品测评内容互动表现好"],
        "complaint_points": [],
        "impact_level": "低",
        "expansion_signals": [],
        "noise_notes": None,
        "key_findings": ["正面声量占主导"],
        "conclusion": "品牌声量稳步上升。",
        "recommendations": ["延续新品测评内容节奏"],
    }


class FakeModel:
    """按 request.output_model 分派：叙事请求校验 narrative dict，其余返回固定 document。"""

    def __init__(self, document: ReportDocument, narrative: dict | Exception | None = None) -> None:
        self.document = document
        self.narrative = narrative if narrative is not None else _narrative_output()
        self.requests: list = []

    async def complete_json(self, request):
        self.requests.append(request)
        if request.output_model is BrandReportNarrative:
            if isinstance(self.narrative, Exception):
                raise self.narrative
            try:
                value = BrandReportNarrative.model_validate(self.narrative)
            except ValidationError as exc:
                raise ModelPlanInvalidError("MODEL_PLAN_INVALID", retryable=False) from exc
            return StructuredResult(
                value=value,
                usage=None,
                request_id="req-test",
                regeneration_count=0,
            )
        return StructuredResult(
            value=self.document,
            usage=None,
            request_id="req-test",
            regeneration_count=0,
        )


def _document() -> ReportDocument:
    return ReportDocument(
        title="品牌声量分析",
        conclusion="品牌声量稳步上升。",
        blocks=[
            HeadingBlock(text="声量概览"),
            MetricGridBlock(items=[MetricItem(label="总声量", value=1200)]),
            ChartBlock(
                type="pie_chart",
                categories=["小红书", "抖音"],
                series=[ChartSeries(name="声量", values=[800, 400])],
            ),
            ChartBlock(
                type="line_chart",
                categories=["06-01", "06-02"],
                series=[ChartSeries(name="声量", values=[500, 700])],
            ),
            TagListBlock(items=["新品", "防晒"]),
            TableBlock(columns=["达人", "声量"], rows=[["达人A", 300]]),
            MarkdownBlock(text="整体表现良好。"),
        ],
    )


TOOL_OVERVIEW = "datatap.insight.social.statistic.overview.v1"


def _plan_json(*notes: dict) -> dict:
    return {"schema": "agent_trajectory_v1", "steps": [], "results": list(notes)}


def _note(tool: str, summary, *, status: str = "settled") -> dict:
    return {"step_id": "step_1", "tool": tool, "status": status, "summary": summary}


def _overview_note() -> dict:
    """品牌 v2 最小证据：当期 overview 指标行（DataTap result 包装）。"""
    rows = [{"平台": "小红书", "声量": 1200, "曝光量": 50000, "互动数": 8000}]
    return _note(TOOL_OVERVIEW, {"result": json.dumps(rows, ensure_ascii=False)})


def test_collect_goal_evidence_extracts_settled_notes() -> None:
    plan_json = _plan_json(
        _note("tool.a", {"total": 100}),
        _note("tool.b", {"total": 0}, status="failed"),
        _note("tool.c", None),
    )

    evidence = collect_goal_evidence(plan_json)

    assert evidence == [{"tool": "tool.a", "structured_content": {"total": 100}}]


def test_collect_goal_evidence_sanitizes_and_truncates() -> None:
    plan_json = _plan_json(_note("tool.a", {"text": "长" * 7000, "url": "https://secret.example.com/x"}))

    [item] = collect_goal_evidence(plan_json)

    content = item["structured_content"]
    encoded = str(content)
    assert len(encoded) <= 6100
    assert "secret.example.com" not in encoded


def test_collect_goal_evidence_empty_inputs() -> None:
    assert collect_goal_evidence(None) == []
    assert collect_goal_evidence({}) == []
    assert collect_goal_evidence({"schema": "agent_trajectory_v1", "results": []}) == []
    assert collect_goal_evidence("not-a-dict") == []


async def _create_session(db_session, user_factory) -> tuple[str, str]:
    user = await user_factory()
    now = datetime.now(UTC).replace(tzinfo=None)
    session = WorkspaceSession(
        id=str(uuid4()),
        user_id=user.id,
        title="构建器测试会话",
        brand="海底捞",
        campaign_name=None,
        status="active",
        platforms=["xiaohongshu"],
        category="美食",
        target_audience="",
        budget_min=None,
        budget_max=None,
        filters_snapshot={},
        is_starred=False,
        last_accessed_at=now,
        created_at=now,
        updated_at=now,
    )
    db_session.add(session)
    await db_session.flush()
    return user.id, session.id


def _task(plan_json) -> SimpleNamespace:
    return SimpleNamespace(id=str(uuid4()), plan_json=plan_json)


def _goal(params: dict) -> SimpleNamespace:
    return SimpleNamespace(id=str(uuid4()), params_json=params)


@pytest.mark.asyncio
async def test_run_brand_analysis_persists_report_with_scope(db_session, user_factory) -> None:
    user_id, session_id = await _create_session(db_session, user_factory)
    model = FakeModel(_document())
    task = _task(_plan_json(_overview_note()))
    goal = _goal(
        {
            "brand": "海底捞",
            "category": "美食",
            "period": {"start": "2026-06-01", "end": "2026-06-30"},
            "platforms": ["xiaohongshu"],
            "campaign": None,
        }
    )

    report = await run_brand_analysis(
        db_session,
        model,
        user_id=user_id,
        session_id=session_id,
        task=task,
        goal=goal,
    )

    assert report.report_type == "brand_analysis"
    assert report.version == 1
    assert report.task_id is None
    assert report.scope_json == {
        "brand": "海底捞",
        "period": {"start": "2026-06-01", "end": "2026-06-30"},
        "platforms": ["xiaohongshu"],
    }
    # v2 落库：快照 + 模板版本 + 兼容 Block + 叙事结论。
    assert report.template_version == "brand_report_v2"
    payload = BrandReportPayload.model_validate(report.payload_json)
    assert payload.narrative is not None
    assert payload.narrative.conclusion == "品牌声量稳步上升。"
    assert payload.data.overview.total_mentions.current == 1200.0
    assert report.conclusion_text == "品牌声量稳步上升。"
    assert report.blocks_json
    block_types = {block["type"] for block in report.blocks_json}
    assert "metric_grid" in block_types
    assert "markdown" in block_types
    assert "sources" in block_types
    # 叙事请求契约。
    [request] = model.requests
    assert request.purpose == "brand_report_narrative"
    assert request.template_name == "brand_report_narrative_v1"
    assert request.output_model is BrandReportNarrative
    assert request.log_context["tags"] == ["brand_report_narrative"]
    assert request.log_context["task_id"] == task.id
    assert request.log_context["user_id"] == user_id
    assert request.log_context["session_id"] == session_id


@pytest.mark.asyncio
async def test_run_brand_analysis_merges_warning_into_payload_availability(
    db_session, user_factory
) -> None:
    """warning_code 经 assemble_brand_report 合并进 availability 对应章节 reason。"""
    user_id, session_id = await _create_session(db_session, user_factory)
    model = FakeModel(_document())

    report = await run_brand_analysis(
        db_session,
        model,
        user_id=user_id,
        session_id=session_id,
        task=_task(_plan_json(_overview_note())),
        goal=_goal({"brand": "海底捞"}),
        warning_code="brand_trend_data_unavailable",
    )

    trend = report.payload_json["availability"]["daily_trend"]
    assert trend["status"] == "unavailable"
    assert "brand_trend_data_unavailable" in (trend["reason"] or "")


@pytest.mark.asyncio
async def test_run_brand_analysis_narrative_error_propagates_without_report(
    db_session, user_factory
) -> None:
    """叙事模型失败原样上抛：不落成 partial 报告（由 finalize 降级收尾）。"""
    user_id, session_id = await _create_session(db_session, user_factory)
    model = FakeModel(
        _document(),
        narrative=ModelPlanInvalidError("MODEL_PLAN_INVALID", retryable=False),
    )

    with pytest.raises(ModelAdapterError):
        await run_brand_analysis(
            db_session,
            model,
            user_id=user_id,
            session_id=session_id,
            task=_task(_plan_json(_overview_note())),
            goal=_goal({"brand": "海底捞"}),
        )

    persisted = await db_session.scalar(
        select(func.count(AnalysisReport.id)).where(AnalysisReport.session_id == session_id)
    )
    assert persisted == 0


@pytest.mark.asyncio
async def test_run_campaign_analysis_versions_independently(db_session, user_factory) -> None:
    user_id, session_id = await _create_session(db_session, user_factory)
    model = FakeModel(_document())
    task = _task(_plan_json(_overview_note()))
    brand_goal = _goal({"brand": "海底捞"})
    campaign_goal = _goal({"brand": "海底捞", "campaign": "618大促"})
    campaign_sink = object()

    brand_v1 = await run_brand_analysis(
        db_session, model, user_id=user_id, session_id=session_id, task=task, goal=brand_goal
    )
    campaign_v1 = await run_campaign_analysis(
        db_session,
        model,
        user_id=user_id,
        session_id=session_id,
        task=task,
        goal=campaign_goal,
        thinking_sink=campaign_sink,
    )
    brand_v2 = await run_brand_analysis(
        db_session, model, user_id=user_id, session_id=session_id, task=task, goal=brand_goal
    )

    assert (brand_v1.version, brand_v2.version) == (1, 2)
    assert campaign_v1.version == 1
    assert campaign_v1.report_type == "campaign_analysis"
    # campaign 路径不变：模型直出 ReportDocument，不写 payload/template_version。
    assert campaign_v1.template_version is None
    assert campaign_v1.payload_json is None
    assert campaign_v1.scope_json == {"brand": "海底捞", "campaign": "618大促"}
    campaign_request = model.requests[1]
    assert campaign_request.purpose == "campaign_analysis"
    assert campaign_request.thinking_sink is campaign_sink
    assert campaign_request.template_name == "campaign_analysis_v1"
    assert campaign_request.log_context["tags"] == ["campaign_analysis"]


@pytest.mark.asyncio
async def test_run_brand_analysis_rejects_empty_evidence(db_session, user_factory) -> None:
    user_id, session_id = await _create_session(db_session, user_factory)
    model = FakeModel(_document())
    task = _task(_plan_json(_note(TOOL_OVERVIEW, None, status="failed")))

    with pytest.raises(LookupError, match="no_evidence_collected"):
        await run_brand_analysis(
            db_session,
            model,
            user_id=user_id,
            session_id=session_id,
            task=task,
            goal=_goal({"brand": "海底捞"}),
        )
    assert model.requests == []


@pytest.mark.asyncio
async def test_run_campaign_analysis_rejects_empty_evidence(db_session, user_factory) -> None:
    user_id, session_id = await _create_session(db_session, user_factory)
    model = FakeModel(_document())

    with pytest.raises(LookupError, match="no_evidence_collected"):
        await run_campaign_analysis(
            db_session,
            model,
            user_id=user_id,
            session_id=session_id,
            task=_task(None),
            goal=_goal({"brand": "海底捞", "campaign": "618"}),
        )


def _payload_for_compat() -> BrandReportPayload:
    """仅 overview 证据的最小快照 + 固定叙事（无情感/趋势/热帖数据）。"""
    payload = assemble_brand_report(
        _plan_json(_overview_note()),
        {"brand": "海底捞", "period": {"start": "2026-06-01", "end": "2026-06-30"}},
    )
    narrative = BrandReportNarrative.model_validate(_narrative_output())
    return payload.model_copy(update={"narrative": narrative})


def test_build_brand_compat_document_omits_blocks_without_data() -> None:
    """兼容文档：缺数据的块（pie/line/table）整块省略，metric_grid/markdown/sources 保留。"""
    payload = _payload_for_compat()

    document = build_brand_compat_document(payload)

    assert document.title == "海底捞 品牌分析报告"
    assert document.conclusion == "品牌声量稳步上升。"
    block_types = [block.type for block in document.blocks]
    assert block_types == ["metric_grid", "markdown", "sources"]
    grid = document.blocks[0]
    assert isinstance(grid, MetricGridBlock)
    values = {item.label: item.value for item in grid.items}
    assert values["总声量"] == 1200.0
    assert values["总互动"] == 8000.0
    assert values["覆盖平台"] == 1
    assert values["时间窗"] == "2026-06-01~2026-06-30"
