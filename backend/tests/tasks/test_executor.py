from types import SimpleNamespace

from app.tasks.executor import (
    aggregate_mcp_progress,
    build_tool_event_payload,
    replan_retry_budget,
    summarize_mcp_failures,
)


def test_tool_event_payload_exposes_only_safe_platform_and_progress() -> None:
    payload = build_tool_event_payload(
        "datatap.douyin.kol.search.v1",
        status="failed",
        step_index=2,
        step_total=3,
        error_code="upstream_error",
        evidence_kind="kol",
    )

    assert payload == {
        "platform": "抖音",
        "evidence_kind": "kol",
        "step_index": 2,
        "step_total": 3,
        "error_code": "upstream_error",
        "message": "社媒数据服务暂时不可用，请稍后重试。",
    }
    assert "datatap" not in str(payload)


def test_parallel_mcp_progress_counts_actual_results_by_platform() -> None:
    commands = (
        SimpleNamespace(internal_tool_name="datatap.xiaohongshu.kol.search.v1"),
        SimpleNamespace(internal_tool_name="datatap.douyin.kol.search.v1"),
        SimpleNamespace(internal_tool_name="datatap.douyin.kol.search.v1"),
    )
    rows = (
        SimpleNamespace(status="settled", internal_tool_name=commands[0].internal_tool_name),
        SimpleNamespace(status="failed", internal_tool_name=commands[1].internal_tool_name, error_type="upstream_error"),
        SimpleNamespace(status="unknown", internal_tool_name=commands[2].internal_tool_name, error_type="possibly_sent_timeout"),
    )

    progress = aggregate_mcp_progress(commands, rows)

    assert progress["step_total"] == 3
    assert progress["step_index"] == 3
    assert progress["platforms"] == {"小红书": {"succeeded": 1, "failed": 0, "unknown": 0}, "抖音": {"succeeded": 0, "failed": 1, "unknown": 1}}


def test_replan_budget_replaces_failed_calls_even_when_original_plan_has_ten_steps() -> None:
    rows = (
        SimpleNamespace(status="released", error_type="upstream_tool_error"),
        SimpleNamespace(status="settled", error_type=None),
        SimpleNamespace(status="unknown", error_type="possibly_sent_timeout"),
    )

    assert replan_retry_budget(rows, max_calls=10) == 1
    assert replan_retry_budget(rows, max_calls=0) == 0


def test_failure_summary_is_safe_and_names_platform_without_internal_details() -> None:
    rows = (
        SimpleNamespace(
            status="released",
            internal_tool_name="datatap.insight.social.statistic.trend.v1",
            error_type="upstream_tool_error",
        ),
        SimpleNamespace(
            status="unknown",
            internal_tool_name="datatap.douyin.kol.search.v1",
            error_type="possibly_sent_timeout",
        ),
    )

    summary = summarize_mcp_failures(rows)

    assert "品牌" not in summary
    assert "社媒平台" in summary
    assert "抖音" in summary
    assert "datatap" not in summary
    assert "upstream_tool_error" not in summary
