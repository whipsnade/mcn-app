from decimal import Decimal

import pytest

from app.orchestration.routing import classify_analysis_request
from app.orchestration.schemas import SessionBrief, ToolPlanStep


def _brief(brand: str = "科颜氏") -> SessionBrief:
    return SessionBrief(
        session_id="session-1",
        brand=brand,
        campaign_name=None,
        platforms=("xiaohongshu", "douyin"),
        category="美妆",
        target_audience="",
        budget_min=Decimal("0"),
        budget_max=None,
        filters={},
    )


def test_brand_volume_question_routes_to_brand() -> None:
    result = classify_analysis_request(
        "分析科颜氏最近3个月在各平台的声量变化和用户情感趋势", _brief()
    )

    assert result.scope == "brand"
    assert set(result.objectives) >= {"volume_trend", "sentiment_trend"}
    assert result.requested_period["unit"] == "month"
    assert result.requested_period["value"] == 3


def test_brand_question_that_requests_active_creators_routes_to_hybrid() -> None:
    result = classify_analysis_request("分析科颜氏声量并找出相关活跃达人", _brief())

    assert result.scope == "hybrid"
    assert set(result.objectives) >= {"brand_analysis", "kol_discovery"}


def test_plain_creator_query_routes_to_kol() -> None:
    result = classify_analysis_request("找最近30天活跃top10达人", _brief())

    assert result.scope == "kol"
    assert "kol_discovery" in result.objectives


def test_legacy_tool_plan_step_defaults_to_kol_evidence() -> None:
    step = ToolPlanStep.model_validate(
        {
            "id": "step_1",
            "internal_tool_name": "x",
            "arguments": {},
            "evidence_goal": "x",
        }
    )

    assert step.evidence_kind == "kol"
