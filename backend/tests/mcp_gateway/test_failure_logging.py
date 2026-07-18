from types import SimpleNamespace

from app.mcp_gateway.service import log_mcp_call_failure


def test_each_failed_call_writes_only_safe_failure_metadata(caplog) -> None:
    row = SimpleNamespace(
        task_id="task-1",
        plan_step_id="step_2",
        status="released",
        error_type="upstream_tool_error",
    )

    with caplog.at_level("WARNING", logger="app.mcp_gateway.service"):
        log_mcp_call_failure(row)

    assert "mcp call failed" in caplog.text
    assert "task-1" in caplog.text
    assert "step_2" in caplog.text
    assert "upstream_tool_error" in caplog.text
    assert "arguments" not in caplog.text
    assert "token" not in caplog.text


def test_successful_call_is_not_logged_as_failure(caplog) -> None:
    row = SimpleNamespace(
        task_id="task-1",
        plan_step_id="step_1",
        status="settled",
        error_type=None,
    )

    with caplog.at_level("WARNING", logger="app.mcp_gateway.service"):
        log_mcp_call_failure(row)

    assert not caplog.records
