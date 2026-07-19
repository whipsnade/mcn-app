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
