from types import SimpleNamespace

from app.tasks.executor import build_tool_event_payload, aggregate_mcp_progress


def test_tool_event_payload_exposes_only_safe_platform_and_progress() -> None:
    payload = build_tool_event_payload(
        "datatap.douyin.kol.search.v1",
        status="failed",
        step_index=2,
        step_total=3,
        error_code="upstream_error",
    )

    assert payload == {
        "platform": "抖音",
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
