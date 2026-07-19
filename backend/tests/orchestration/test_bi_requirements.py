import pytest

from app.orchestration.bi_requirements import (
    BI_REQUIRED_METRICS,
    metric_coverage,
    missing_metrics,
    required_metrics_payload,
)
from app.orchestration.loop import AgentTrajectory, EvidenceNote


_OVERVIEW_TOOL = "datatap.insight.social.statistic.overview.v1"
_ANALYSIS_TOOL = "datatap.insight.query.analysis.v1"
_TREND_TOOL = "datatap.insight.social.statistic.trend.v1"


def _note(tool: str, status: str = "settled", summary=None) -> EvidenceNote:
    return EvidenceNote(step_id="step_1", tool=tool, status=status, summary=summary)


def _trajectory(*notes: EvidenceNote) -> AgentTrajectory:
    return AgentTrajectory(results=list(notes))


def test_required_metrics_define_all_eight_bi_items() -> None:
    keys = [metric.key for metric in BI_REQUIRED_METRICS]
    assert keys == [
        "brand_voice",
        "exposure",
        "engagement",
        "sentiment",
        "hot_words",
        "voice_trend",
        "audience_profile",
        "kol_leaderboard",
    ]
    for metric in BI_REQUIRED_METRICS:
        assert metric.label
        assert metric.description
        assert metric.source_tools


def test_required_metrics_payload_is_serializable_shape() -> None:
    payload = required_metrics_payload()
    assert len(payload) == 8
    first = payload[0]
    assert set(first) == {"key", "label", "description", "source_tools"}
    assert first["key"] == "brand_voice"
    assert isinstance(first["source_tools"], list)


def test_settled_call_covers_mapped_metrics() -> None:
    coverage = metric_coverage(_trajectory(_note(_OVERVIEW_TOOL, summary={"total": 1})))

    assert {"brand_voice", "exposure"} <= coverage.covered
    assert coverage.attempted_empty == frozenset()
    missing_keys = [metric.key for metric in missing_metrics(coverage)]
    assert "brand_voice" not in missing_keys
    assert "engagement" in missing_keys


@pytest.mark.parametrize("summary", [None, {}, [], "null", "{}", "[]", "  {}  "])
def test_settled_empty_summary_marks_attempted_empty_without_blocking(summary) -> None:
    # overview 同时映射 brand_voice 与 exposure：空结果两项都记 attempted_empty。
    coverage = metric_coverage(_trajectory(_note(_OVERVIEW_TOOL, summary=summary)))

    assert coverage.covered == frozenset()
    assert coverage.attempted_empty == frozenset({"brand_voice", "exposure"})
    missing_keys = [metric.key for metric in missing_metrics(coverage)]
    assert "brand_voice" not in missing_keys
    assert "exposure" not in missing_keys


def test_failed_call_does_not_cover() -> None:
    coverage = metric_coverage(
        _trajectory(_note(_OVERVIEW_TOOL, status="failed", summary="上游错误"))
    )

    assert coverage.covered == frozenset()
    assert coverage.attempted_empty == frozenset()
    assert len(missing_metrics(coverage)) == len(BI_REQUIRED_METRICS)


def test_later_non_empty_call_upgrades_attempted_empty_to_covered() -> None:
    coverage = metric_coverage(
        _trajectory(
            _note(_OVERVIEW_TOOL, summary={}),
            _note(_OVERVIEW_TOOL, summary={"total": 1}),
        )
    )

    assert {"brand_voice", "exposure"} <= coverage.covered
    assert coverage.attempted_empty == frozenset()


def test_unknown_tool_does_not_cover_anything() -> None:
    coverage = metric_coverage(_trajectory(_note("datatap.unknown.tool", summary={"a": 1})))

    assert coverage.covered == frozenset()
    assert len(missing_metrics(coverage)) == len(BI_REQUIRED_METRICS)


def test_non_empty_string_summary_covers() -> None:
    # sanitize_evidence 超长时退化为截断字符串，非空字符串视为有数据。
    coverage = metric_coverage(_trajectory(_note(_TREND_TOOL, summary='{"points":[1,2]}')))

    assert "voice_trend" in coverage.covered
    assert "voice_trend" not in [metric.key for metric in missing_metrics(coverage)]


def test_missing_metrics_returns_defs_in_checklist_order() -> None:
    coverage = metric_coverage(
        _trajectory(_note(_ANALYSIS_TOOL, summary={"voice": 1}))
    )

    missing = missing_metrics(coverage)
    missing_keys = [metric.key for metric in missing]
    # analysis 工具覆盖 brand_voice/exposure/engagement/sentiment 四项。
    assert missing_keys == [
        "hot_words",
        "voice_trend",
        "audience_profile",
        "kol_leaderboard",
    ]
