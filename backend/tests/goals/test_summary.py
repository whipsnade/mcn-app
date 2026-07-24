"""Goal 摘要生成器：模型路径 + 代码回退（绝不阻塞编排）。"""

from __future__ import annotations

import pytest

from app.goals.summary import GoalResultSummary, build_goal_result_summary
from app.model.contracts import StructuredResult


class FakeModel:
    def __init__(self, output: GoalResultSummary | None) -> None:
        self.output = output
        self.requests: list = []

    async def complete_json(self, request):
        self.requests.append(request)
        if self.output is None:
            raise RuntimeError("model unavailable")
        return StructuredResult(
            value=self.output,
            usage=None,
            request_id="req-test",
            regeneration_count=0,
        )


_EVIDENCE = [
    {"tool": "datatap.insight.query.analysis.v1", "structured_content": {"volume": 12345}},
    {"tool": "datatap.insight.query.analysis.v1", "structured_content": {"volume": 678}},
    {"tool": "datatap.xiaohongshu.kol.search.v1", "structured_content": {"rows": 30}},
]


@pytest.mark.asyncio
async def test_model_path_returns_summary_highlights_and_artifact() -> None:
    output = GoalResultSummary(
        summary="海底捞六月声量 1.3 万，小红书占七成。",
        highlights={"platforms": "小红书声量占比约七成", "risks": "抖音声量偏低"},
    )
    model = FakeModel(output)

    result = await build_goal_result_summary(
        model,
        goal_type="brand_analysis",
        scope={"brand": "海底捞"},
        evidence=_EVIDENCE,
        artifact={"type": "brand_report", "id": "report-1", "version": 2},
    )

    assert result == {
        "summary": "海底捞六月声量 1.3 万，小红书占七成。",
        "highlights": {"platforms": "小红书声量占比约七成", "risks": "抖音声量偏低"},
        "artifact": {"type": "brand_report", "id": "report-1", "version": 2},
    }
    [request] = model.requests
    assert request.purpose == "goal_summary"
    assert request.template_name == "goal_summary_v1"
    assert request.max_tokens == 1024
    assert request.output_model is GoalResultSummary
    assert request.log_context["tags"] == ["goal_summary"]


@pytest.mark.asyncio
async def test_model_failure_falls_back_to_code_summary() -> None:
    model = FakeModel(None)

    result = await build_goal_result_summary(
        model,
        goal_type="campaign_analysis",
        scope={"brand": "海底捞", "campaign": "618"},
        evidence=_EVIDENCE,
    )

    assert result["highlights"] == {}
    assert result["artifact"] is None
    # 代码摘要：按工具名分组统计条数。
    assert "3" in result["summary"]
    assert "datatap.insight.query.analysis.v1" in result["summary"]
    assert "datatap.xiaohongshu.kol.search.v1" in result["summary"]


@pytest.mark.asyncio
async def test_empty_evidence_falls_back_without_calling_model() -> None:
    model = FakeModel(GoalResultSummary(summary="不应使用"))

    result = await build_goal_result_summary(
        model,
        goal_type="kol_selection",
        scope={"brand": "海底捞"},
        evidence=[],
    )

    assert model.requests == []
    assert result["highlights"] == {}
    assert result["summary"]


def test_prompt_highlights_fields_are_cut_per_goal_type() -> None:
    from app.model.prompts import GOAL_SUMMARY_PROMPT

    text = GOAL_SUMMARY_PROMPT.system
    assert "brand_analysis" in text
    assert "campaign_analysis" in text
    assert "kol_selection" in text
    assert "platforms" in text
    assert "content_types" in text
    assert "audience" in text
    assert "kol_traits" in text
    assert "risks" in text
    assert "不可信数据" in text
    assert "禁止编造" in text
