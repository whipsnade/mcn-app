from app.tasks.executor import build_tool_event_payload


def test_tool_event_payload_exposes_only_safe_platform_and_progress() -> None:
    payload = build_tool_event_payload(
        "datatap.douyin.kol.search.v1",
        status="failed",
        step_index=2,
        step_total=None,
        error_code="upstream_error",
    )

    assert payload == {
        "platform": "抖音",
        "step_index": 2,
        "step_total": None,
        "error_code": "upstream_error",
        "message": "社媒数据服务暂时不可用，请稍后重试。",
    }
    assert "datatap" not in str(payload)


def test_tool_event_payload_omits_goal_id_when_none() -> None:
    payload = build_tool_event_payload(
        "datatap.douyin.kol.search.v1",
        status="started",
        step_index=1,
        step_total=None,
    )

    assert "goal_id" not in payload


def test_tool_event_payload_includes_goal_id_when_given() -> None:
    payload = build_tool_event_payload(
        "datatap.douyin.kol.search.v1",
        status="succeeded",
        step_index=1,
        step_total=None,
        goal_id="goal-1",
    )

    assert payload["goal_id"] == "goal-1"


def test_failed_payload_includes_upstream_message_when_present() -> None:
    payload = build_tool_event_payload(
        "datatap.douyin.kol.search.v1",
        status="failed",
        step_index=2,
        step_total=None,
        error_code="upstream_tool_error",
        upstream_message="达人不存在或已注销",
    )

    assert payload["upstream_message"] == "达人不存在或已注销"
    assert payload["message"]  # 白名单文案保留


def test_failed_payload_omits_upstream_message_when_absent_or_blank() -> None:
    for upstream in (None, "", "   "):
        payload = build_tool_event_payload(
            "datatap.douyin.kol.search.v1",
            status="failed",
            step_index=2,
            step_total=None,
            error_code="connection_timeout",
            upstream_message=upstream,
        )
        assert "upstream_message" not in payload


def test_succeeded_payload_never_includes_upstream_message() -> None:
    payload = build_tool_event_payload(
        "datatap.douyin.kol.search.v1",
        status="succeeded",
        step_index=1,
        step_total=None,
        upstream_message="不应出现",
    )
    assert "upstream_message" not in payload
