"""brand_report_v2 叙事层测试：结构化数据唯一输入的模型撰写（Task 5）。

FakeModel 模拟 TencentPlanAdapter.complete_json 的出口行为：原始 dict 输出
经 request.output_model 校验，失败抛 ModelPlanInvalidError；全程不调真实模型。
"""

from __future__ import annotations

import json

from pydantic import ValidationError
import pytest

from app.model.contracts import ModelPlanInvalidError, StructuredResult
from app.reporting.brand_narrative import BrandReportNarrative, build_brand_narrative
from app.reporting.brand_payload import (
    ALL_CHAPTERS,
    BrandReportData,
    BrandReportPayload,
    ChapterAvailability,
    MetricComparison,
    OverviewSection,
    PlatformOverview,
    QuerySpec,
    ReportScope,
    SourceEntry,
)


class FakeModel:
    def __init__(self, outputs: list[dict]) -> None:
        self.outputs = list(outputs)
        self.requests: list = []

    async def complete_json(self, request):
        self.requests.append(request)
        raw = self.outputs.pop(0)
        try:
            value = request.output_model.model_validate(raw)
        except ValidationError as exc:
            raise ModelPlanInvalidError("MODEL_PLAN_INVALID", retryable=False) from exc
        return StructuredResult(
            value=value,
            usage=None,
            request_id="brand-narrative-test",
            regeneration_count=0,
        )


def _payload() -> BrandReportPayload:
    return BrandReportPayload(
        data_status="partial",
        scope=ReportScope(
            brand="肯德基",
            period_start="2026-06-01",
            period_end="2026-06-30",
            platforms=["小红书"],
            comparison_mode="mom",
        ),
        query_spec=QuerySpec(
            original_term="肯德基",
            matched_tag="肯德基",
            comparison_definition="环比 2026-05-02~2026-05-31",
        ),
        data=BrandReportData(
            overview=OverviewSection(
                platforms=[PlatformOverview(platform="xiaohongshu", mentions=1000.0)],
                total_mentions=MetricComparison(current=1000.0, mom_change_pct=100.0),
            )
        ),
        availability={
            chapter: ChapterAvailability(status="complete") for chapter in ALL_CHAPTERS
        },
        sources=[SourceEntry(tool="datatap.insight.social.statistic.overview.v1", step_id="step_1")],
    )


def _valid_output() -> dict:
    return {
        "praise_points": ["新品测评内容互动表现好"],
        "complaint_points": ["部分门店服务吐槽"],
        "impact_level": "中",
        "expansion_signals": ["下沉市场声量上升"],
        "noise_notes": None,
        "key_findings": ["正面声量占主导"],
        "conclusion": "品牌整体声量环比增长，情感偏正面。",
        "recommendations": ["延续新品测评内容节奏"],
    }


@pytest.mark.asyncio
async def test_model_input_only_contains_data_and_availability() -> None:
    model = FakeModel([_valid_output()])

    await build_brand_narrative(model, _payload(), log_context={"user_id": "u1"})

    request = model.requests[0]
    assert request.messages[0].role == "system"
    content = request.messages[-1].content
    user = json.loads(content)
    # 模型输入只有 scope/query_spec/data/availability 四键：不含原始 evidence、
    # 不含 sources 内部 step_id；scope/query_spec 提供主语与时间窗（无数值指标）。
    assert set(user) == {"scope", "query_spec", "data", "availability"}
    assert "evidence" not in user
    assert "sources" not in user
    assert "step_id" not in content
    assert user["scope"]["brand"] == "肯德基"
    assert user["query_spec"]["matched_tag"] == "肯德基"
    assert user["data"]["overview"]["total_mentions"]["current"] == 1000.0
    assert set(user["availability"]) == set(ALL_CHAPTERS)


@pytest.mark.asyncio
async def test_valid_output_parses_to_brand_report_narrative() -> None:
    model = FakeModel([_valid_output()])

    narrative = await build_brand_narrative(model, _payload(), log_context={})

    assert isinstance(narrative, BrandReportNarrative)
    assert narrative.praise_points == ["新品测评内容互动表现好"]
    assert narrative.impact_level == "中"
    assert narrative.noise_notes is None
    assert narrative.conclusion == "品牌整体声量环比增长，情感偏正面。"
    assert narrative.recommendations == ["延续新品测评内容节奏"]


@pytest.mark.asyncio
async def test_extra_field_in_model_output_raises_validation_error() -> None:
    output = {**_valid_output(), "hallucinated_metric": 12345}
    model = FakeModel([output])

    with pytest.raises(ModelPlanInvalidError, match="MODEL_PLAN_INVALID"):
        await build_brand_narrative(model, _payload(), log_context={})


@pytest.mark.asyncio
async def test_invalid_impact_level_raises_validation_error() -> None:
    output = {**_valid_output(), "impact_level": "严重"}
    model = FakeModel([output])

    with pytest.raises(ModelPlanInvalidError, match="MODEL_PLAN_INVALID"):
        await build_brand_narrative(model, _payload(), log_context={})


@pytest.mark.asyncio
async def test_empty_conclusion_raises_validation_error() -> None:
    output = {**_valid_output(), "conclusion": ""}
    model = FakeModel([output])

    with pytest.raises(ModelPlanInvalidError, match="MODEL_PLAN_INVALID"):
        await build_brand_narrative(model, _payload(), log_context={})


@pytest.mark.asyncio
async def test_overlong_conclusion_raises_validation_error() -> None:
    """conclusion 超 4000 字（ReportDocument.conclusion 上限）→ 校验异常上抛。"""
    output = {**_valid_output(), "conclusion": "长" * 4001}
    model = FakeModel([output])

    with pytest.raises(ModelPlanInvalidError, match="MODEL_PLAN_INVALID"):
        await build_brand_narrative(model, _payload(), log_context={})


@pytest.mark.asyncio
async def test_request_purpose_and_log_tags() -> None:
    model = FakeModel([_valid_output()])

    await build_brand_narrative(
        model,
        _payload(),
        log_context={"user_id": "u1", "session_id": "s1", "task_id": "t1", "tags": ["caller"]},
    )

    request = model.requests[0]
    assert request.purpose == "brand_report_narrative"
    assert request.template_name == "brand_report_narrative_v1"
    assert request.output_model is BrandReportNarrative
    assert request.log_context["user_id"] == "u1"
    assert request.log_context["session_id"] == "s1"
    assert request.log_context["task_id"] == "t1"
    assert "brand_report_narrative" in request.log_context["tags"]
    # 调用方已有 tags 保留
    assert "caller" in request.log_context["tags"]
