"""品牌/活动报告构建器：证据聚合 → 模型撰写 → 会话级落库。"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.model.contracts import StructuredResult
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
from app.reporting.builders import (
    collect_goal_evidence,
    run_brand_analysis,
    run_campaign_analysis,
)
from app.workspace.models import WorkspaceSession


class FakeModel:
    def __init__(self, document: ReportDocument) -> None:
        self.document = document
        self.requests: list = []

    async def complete_json(self, request):
        self.requests.append(request)
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


def _plan_json(*notes: dict) -> dict:
    return {"schema": "agent_trajectory_v1", "steps": [], "results": list(notes)}


def _note(tool: str, summary, *, status: str = "settled") -> dict:
    return {"step_id": "step_1", "tool": tool, "status": status, "summary": summary}


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
    task = _task(_plan_json(_note("tool.a", {"volume": 100})))
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
        db_session, model, user_id=user_id, session_id=session_id, task=task, goal=goal
    )

    assert report.report_type == "brand_analysis"
    assert report.version == 1
    assert report.task_id is None
    assert report.scope_json == {
        "brand": "海底捞",
        "period": {"start": "2026-06-01", "end": "2026-06-30"},
        "platforms": ["xiaohongshu"],
    }
    assert report.title == "品牌声量分析"
    [request] = model.requests
    assert request.purpose == "brand_analysis"
    assert request.template_name == "brand_analysis_v1"
    assert request.max_tokens == 8192
    assert request.log_context["tags"] == ["brand_analysis"]
    assert request.log_context["task_id"] == task.id


@pytest.mark.asyncio
async def test_run_campaign_analysis_versions_independently(db_session, user_factory) -> None:
    user_id, session_id = await _create_session(db_session, user_factory)
    model = FakeModel(_document())
    task = _task(_plan_json(_note("tool.a", {"volume": 100})))
    brand_goal = _goal({"brand": "海底捞"})
    campaign_goal = _goal({"brand": "海底捞", "campaign": "618大促"})

    brand_v1 = await run_brand_analysis(
        db_session, model, user_id=user_id, session_id=session_id, task=task, goal=brand_goal
    )
    campaign_v1 = await run_campaign_analysis(
        db_session, model, user_id=user_id, session_id=session_id, task=task, goal=campaign_goal
    )
    brand_v2 = await run_brand_analysis(
        db_session, model, user_id=user_id, session_id=session_id, task=task, goal=brand_goal
    )

    assert (brand_v1.version, brand_v2.version) == (1, 2)
    assert campaign_v1.version == 1
    assert campaign_v1.report_type == "campaign_analysis"
    assert campaign_v1.scope_json == {"brand": "海底捞", "campaign": "618大促"}
    assert model.requests[1].purpose == "campaign_analysis"
    assert model.requests[1].template_name == "campaign_analysis_v1"
    assert model.requests[1].log_context["tags"] == ["campaign_analysis"]


@pytest.mark.asyncio
async def test_run_brand_analysis_rejects_empty_evidence(db_session, user_factory) -> None:
    user_id, session_id = await _create_session(db_session, user_factory)
    model = FakeModel(_document())
    task = _task(_plan_json(_note("tool.a", None, status="failed")))

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
